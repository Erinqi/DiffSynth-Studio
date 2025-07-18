import torch, os, json
import torch.nn.functional as F
from diffsynth.pipelines.wan_video_new import WanVideoPipeline, ModelConfig
from diffsynth.trainers.utils import DiffusionTrainingModule, VideoDataset, ModelLogger, launch_training_task, wan_parser
os.environ["TOKENIZERS_PARALLELISM"] = "false"



class WanTrainingModule(DiffusionTrainingModule):
    def __init__(
        self,
        model_paths=None, model_id_with_origin_paths=None,
        trainable_models=None,
        lora_base_model=None, lora_target_modules="q,k,v,o,ffn.0,ffn.2", lora_rank=32,
        use_gradient_checkpointing=True,
        use_gradient_checkpointing_offload=False,
        extra_inputs=None,
        dpo=False,
        dpo_beta=0.1,
        chosen_key="chosen",
        rejected_key="rejected",
    ):
        super().__init__()
        # Load models
        model_configs = []
        if model_paths is not None:
            model_paths = json.loads(model_paths)
            model_configs += [ModelConfig(path=path) for path in model_paths]
        if model_id_with_origin_paths is not None:
            model_id_with_origin_paths = model_id_with_origin_paths.split(",")
            model_configs += [ModelConfig(model_id=i.split(":")[0], origin_file_pattern=i.split(":")[1]) for i in model_id_with_origin_paths]
        self.pipe = WanVideoPipeline.from_pretrained(torch_dtype=torch.bfloat16, device="cpu", model_configs=model_configs)
        
        # Reset training scheduler
        self.pipe.scheduler.set_timesteps(1000, training=True)
        
        # Freeze untrainable models
        self.pipe.freeze_except([] if trainable_models is None else trainable_models.split(","))
        
        # Add LoRA to the base models
        if lora_base_model is not None:
            model = self.add_lora_to_model(
                getattr(self.pipe, lora_base_model),
                target_modules=lora_target_modules.split(","),
                lora_rank=lora_rank
            )
            setattr(self.pipe, lora_base_model, model)
            
        # Store other configs
        self.use_gradient_checkpointing = use_gradient_checkpointing
        self.use_gradient_checkpointing_offload = use_gradient_checkpointing_offload
        self.extra_inputs = extra_inputs.split(",") if extra_inputs is not None else []
        self.dpo = dpo
        self.dpo_beta = dpo_beta
        self.chosen_key = chosen_key
        self.rejected_key = rejected_key
        
        
    def forward_preprocess(self, data, video_key="video"):
        # CFG-sensitive parameters
        inputs_posi = {"prompt": data["prompt"]}
        inputs_nega = {}
        
        # CFG-unsensitive parameters
        inputs_shared = {
            # Assume you are using this pipeline for inference,
            # please fill in the input parameters.
            "input_video": data[video_key],
            "height": data[video_key][0].size[1],
            "width": data[video_key][0].size[0],
            "num_frames": len(data[video_key]),
            # Please do not modify the following parameters
            # unless you clearly know what this will cause.
            "cfg_scale": 1,
            "tiled": False,
            "rand_device": self.pipe.device,
            "use_gradient_checkpointing": self.use_gradient_checkpointing,
            "use_gradient_checkpointing_offload": self.use_gradient_checkpointing_offload,
            "cfg_merge": False,
            "vace_scale": 1,
        }
        
        # Extra inputs
        for extra_input in self.extra_inputs:
            if extra_input == "input_image":
                inputs_shared["input_image"] = data[video_key][0]
            elif extra_input == "end_image":
                inputs_shared["end_image"] = data[video_key][-1]
            else:
                inputs_shared[extra_input] = data[extra_input]
        
        # Pipeline units will automatically process the input parameters.
        for unit in self.pipe.units:
            inputs_shared, inputs_posi, inputs_nega = self.pipe.unit_runner(unit, self.pipe, inputs_shared, inputs_posi, inputs_nega)
        return {**inputs_shared, **inputs_posi}
    
    
    def forward(self, data, inputs=None):
        if not self.dpo:
            if inputs is None:
                inputs = self.forward_preprocess(data)
            models = {name: getattr(self.pipe, name) for name in self.pipe.in_iteration_models}
            loss = self.pipe.training_loss(**models, **inputs)
            return loss
        else:
            chosen_inputs = self.forward_preprocess({
                "prompt": data["prompt"],
                self.chosen_key: data[self.chosen_key]
            }, video_key=self.chosen_key)
            rejected_inputs = self.forward_preprocess({
                "prompt": data["prompt"],
                self.rejected_key: data[self.rejected_key]
            }, video_key=self.rejected_key)

            models = {name: getattr(self.pipe, name) for name in self.pipe.in_iteration_models}
            loss_chosen = self.pipe.training_loss(**models, **chosen_inputs)
            loss_rejected = self.pipe.training_loss(**models, **rejected_inputs)
            advantage = loss_rejected - loss_chosen
            dpo_loss = -F.logsigmoid(self.dpo_beta * advantage)
            return dpo_loss


if __name__ == "__main__":
    parser = wan_parser()
    args = parser.parse_args()
    if args.dpo:
        args.data_file_keys = f"{args.chosen_video_key},{args.rejected_video_key}"
    dataset = VideoDataset(args=args)
    model = WanTrainingModule(
        model_paths=args.model_paths,
        model_id_with_origin_paths=args.model_id_with_origin_paths,
        trainable_models=args.trainable_models,
        lora_base_model=args.lora_base_model,
        lora_target_modules=args.lora_target_modules,
        lora_rank=args.lora_rank,
        use_gradient_checkpointing_offload=args.use_gradient_checkpointing_offload,
        extra_inputs=args.extra_inputs,
        dpo=args.dpo,
        dpo_beta=args.dpo_beta,
        chosen_key=args.chosen_video_key,
        rejected_key=args.rejected_video_key,
    )
    model_logger = ModelLogger(
        args.output_path,
        remove_prefix_in_ckpt=args.remove_prefix_in_ckpt
    )
    optimizer = torch.optim.AdamW(model.trainable_modules(), lr=args.learning_rate)
    scheduler = torch.optim.lr_scheduler.ConstantLR(optimizer)
    launch_training_task(
        dataset, model, model_logger, optimizer, scheduler,
        num_epochs=args.num_epochs,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
    )
