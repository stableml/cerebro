import os
import torch
import logging
from packaging import version
from collections import defaultdict
from typing import Optional, Dict

from transformers import (
    Trainer,
    PretrainedConfig,
    __version__,
)
from transformers.modeling_utils import unwrap_model
from transformers.models.auto.modeling_auto import MODEL_FOR_CAUSAL_LM_MAPPING_NAMES
from transformers.utils import(
    CONFIG_NAME,
    WEIGHTS_NAME,
    SAFE_WEIGHTS_NAME,
    SAFE_WEIGHTS_INDEX_NAME,
    ADAPTER_WEIGHTS_NAME,
    ADAPTER_SAFE_WEIGHTS_NAME,
    is_accelerate_available,
    is_deepspeed_available,
    is_safetensors_available,
    is_peft_available
)

from ..utils import EarlyStopping 


if is_accelerate_available():
    from accelerate import Accelerator, skip_first_batches
    from accelerate import __version__ as accelerate_version
    from accelerate.utils import DistributedDataParallelKwargs, GradientAccumulationPlugin


    if version.parse(accelerate_version) > version.parse("0.20.3"):
        from accelerate.utils import (
            load_fsdp_model,
            load_fsdp_optimizer,
            save_fsdp_model,
            save_fsdp_optimizer,
        )

    if is_deepspeed_available():
        from accelerate.utils import DeepSpeedSchedulerWrapper
    
        
if is_safetensors_available():
    import safetensors.torch
    
    
if is_peft_available():
    from peft import PeftModel
    

logger = logging.getLogger(__name__)



class BaseTrainer(Trainer):
    def __init__(
        self, 
        early_stopping: Optional[EarlyStopping] = None,
        *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        self.early_stopping = early_stopping
        # Inject Customised logging behavior
        self.customized_logging_list = defaultdict(list)

    def _load_from_checkpoint(self, resume_from_checkpoint, model=None):
        if model is None:
            model = self.model
            
        config_file = os.path.join(resume_from_checkpoint, CONFIG_NAME)
        weights_file = os.path.join(resume_from_checkpoint, WEIGHTS_NAME)
        
        # check using fsdp
        is_fsdp_ckpt = os.path.isdir(resume_from_checkpoint) and any(
            WEIGHTS_NAME.split(".")[0] in folder_name
            for folder_name in os.listdir(resume_from_checkpoint)
            if os.path.isdir(os.path.join(resume_from_checkpoint, folder_name))
        )
        if is_fsdp_ckpt and not self.is_fsdp_enabled:
            raise ValueError(f"Checkpoint found at {resume_from_checkpoint} is only supported when using PyTorch FSDP")
        
        if not(
            any(
                os.path.file(f) for f in [config_file, weights_file]    
            ) or is_fsdp_ckpt
        ):
            raise ValueError(f"Can't find a valid checkpoint at {resume_from_checkpoint}")
        
        # Using safe weight tensor 
        if self.args.save_safetensors:
            safe_weights_file = os.path.join(resume_from_checkpoint, SAFE_WEIGHTS_NAME)
            safe_weights_index_file = os.path.join(resume_from_checkpoint, SAFE_WEIGHTS_INDEX_NAME)
            if not(
                any (os.path.file(f) for f in [safe_weights_file, safe_weights_index_file]) 
            ):
                raise ValueError(f"Can't find safe_weights_file checkpoint at {resume_from_checkpoint}")
            
        # Using PEFT LORA
        if is_peft_available() and isinstance(model, PeftModel):
            adapter_weights_file = os.path.join(resume_from_checkpoint, ADAPTER_WEIGHTS_NAME)
            adapter_safe_weights_file = os.path.join(resume_from_checkpoint, ADAPTER_SAFE_WEIGHTS_NAME)
            if not(
                any (os.path.file(f) for f in [adapter_weights_file, adapter_safe_weights_file]) 
            ):
                raise ValueError(f"Can't find adapter PEFT-LORA checkpoint at {resume_from_checkpoint}")
        
        logger.info(f"Loading checkpoint from {resume_from_checkpoint}.")
        
        # check config file
        if os.path.isfile(config_file):
            config = PretrainedConfig.from_json_file(config_file)
            checkpoint_version = config.transformers_version
            if checkpoint_version is not None and checkpoint_version != __version__:
                logger.warning(
                    f"You are resuming training from a checkpoint trained with {checkpoint_version} of "
                    f"Transformers but your current version is {__version__}. This is not recommended and could "
                    "yield to errors or unwanted behaviors."
                )
        
        if os.path.isfile(weights_file) or os.path.join(safe_weights_file) or is_fsdp_ckpt:
            if self.is_fsdp_enabled:
                load_fsdp_model(self.accelerator.state.fsdp_plugin, self.accelerator, model, resume_from_checkpoint)
            else:
                # We load the model state dict on the CPU to avoid an OOM error.
                if self.args.save_safetensors and os.path.isfile(safe_weights_file):
                    state_dict = safetensors.torch.load_file(safe_weights_file, device="cpu")
                else:
                    state_dict = torch.load(weights_file, map_location="cpu")

                # workaround for FSDP bug https://github.com/pytorch/pytorch/issues/82963
                # which takes *args instead of **kwargs
                load_result = model.load_state_dict(state_dict, False)
                # release memory
                del state_dict
                self._issue_warnings_after_load(load_result)
                
        # Load adapters following PR # 24096
        elif is_peft_available() and isinstance(model, PeftModel):
            # If train a model using PEFT & LoRA, assume that adapter have been saved properly.
            if hasattr(model, "active_adapter") and hasattr(model, "load_adapter"):
                if os.path.exists(resume_from_checkpoint):
                    model.load_adapter(resume_from_checkpoint, model.active_adapter, is_trainable=True)
                else:
                    logger.warning(
                        "The intermediate checkpoints of PEFT may not be saved correctly, "
                        f"consider using a custom callback to save {ADAPTER_WEIGHTS_NAME} in corresponding saving folders. "
                        "Check some examples here: https://github.com/huggingface/peft/issues/96"
                    )
            else:
                logger.warning("Could not load adapter model, make sure to have `peft>=0.3.0` installed")
                
    def compute_loss(self, model, inputs, return_outputs=False):
        """
        How the loss is computed by Trainer. By default, all models return the loss in the first element.
        
        A neat compute_loss that supports customized logging

        """
        if self.label_smoother is not None and "labels" in inputs:
            labels = inputs.pop("labels")
        else:
            labels = None
            
        outputs = model(**inputs)
        
        # Save past state if it exists
        # TODO: this needs to be fixed and made cleaner later.
        if self.args.past_index >= 0:
            self._past = outputs[self.args.past_index]
            
        if labels is not None:
            if is_peft_available() and isinstance(model, PeftModel):
                model_name = unwrap_model(model.base_model)._get_name()
            else:
                model_name = unwrap_model(model)._get_name()
            if model_name in MODEL_FOR_CAUSAL_LM_MAPPING_NAMES.values():
                loss = self.label_smoother(outputs, labels, shift_labels=True)
            else:
                loss = self.label_smoother(outputs, labels)
        else:
            loss = outputs["loss"] if isinstance(outputs, dict) else outputs[0]
            try:
                logs:dict  = outputs.logs
            except:
                logs = None
                
            if logs is not None:
                for key, value in logs.items():
                    # Set maxlen of list to avoid memory leak, useful when
                    # customized_logging_list has not been cleaned correctly
                    if len(self.customized_logging_list) < 5000:
                        self.customized_logging_list[key].append(value)
                        
        return (loss, outputs) if return_outputs else loss
        
                
    def log(self, logs: Dict[str, float]) -> None:
        """
        Log `logs` on the various objects watching training.

        Subclass and override this method to inject custom behavior.

        Args:
            logs (`Dict[str, float]`):
                The values to log.
        """
        
        if self.state.epoch is not None:
            logs['epoch'] = round(self.state.epoch, 2)
            
        # Inject Customised logging behavior
        for k, v in self.customized_logging_list.items():
            if len(v) > 0:
                if isinstance(v[0], torch.Tensor):
                    v = [value.items() for value in v]
                logs[k] = round(sum(v) / len(v), 4)
              
        self.customized_logging_list.clear()

        output = {**logs, **{"step": self.state.global_step}}
        
        self.state.log_history.append(output)
        self.control = self.callback_handler.on_log(self.args, self.state, self.control, logs)
        

        
        
        
        
        
        