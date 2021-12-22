import torch
import torch.nn.functional as F
from torchtyping import TensorType, patch_typeguard
from typeguard import typechecked
from carp.configs import CARPConfig, ModelConfig, TrainConfig
from carp.pytorch.model.architectures import * 
from carp.pytorch.model.encoders import get_encoder
from carp.util import mbTokens, generate_indices
from typing import List


# CARP MLM differs from normal CARP since the first epoch will solely use an MLM objective to improve data efficiency. 
# TODO: The learning rate scheduler needs to account for this, so we need a way to register custom LR schedulers.
# TODO: We need to make sure it saves a CARP MLM checkpoint after the first epoch so that we can convert it to CARP Cloob or CARP momentum

patch_typeguard()

@typechecked
@register_architecture("CARP MLM")
class CARPMLM(ContrastiveModel):
    def __init__(self, config: ModelConfig):
        super().__init__()
        self.config = config
        encoder_class = get_encoder(config.encoder_type)
        self.passage_encoder = encoder_class(
            config.model_path, config.model_arch
        )
        self.review_encoder = encoder_class(
            config.model_path, config.model_arch
        )
        self.latent_dim = self.config.latent_dim
        self.pass_projector, self.rev_projector = self._make_projection_layers(self.config)
        self.logit_scale = nn.Parameter(
            torch.ones([], device=self.config.device)
            * torch.log(torch.tensor([1 / 0.07], device=self.config.device))
        )
        self.clamp_min = torch.log(torch.tensor([1 / 100], device=self.config.device))
        self.clamp_max = torch.log(torch.tensor([100], device=self.config.device))

    def train_step(
        self,
        passages: List[TensorType["batch", "N_pass"]],
        reviews: List[TensorType["batch", "N_rev"]],
        config: TrainConfig,
        opt: torch.optim.Optimizer,
        scaler: torch.cuda.amp.GradScaler,
    ) -> Dict[str, TensorType[()]]:
        microbatch_inds = generate_indices(
            passages[0].shape[0], config.microbatch_size, shuffle=False
        )
        # Split tokens and masks into these microbatches
        pass_mbs: List[Tuple[mbTokens, mbTokens]] = [
            (passages[0][i], passages[1][i]) for i in microbatch_inds
        ]
        rev_mbs: List[Tuple[mbTokens, mbTokens]] = [
            (reviews[0][i], reviews[1][i]) for i in microbatch_inds
        ]
        # Initially get all encodings without grad
        pass_encs, rev_encs = self.calculate_embeddings(pass_mbs, rev_mbs)

        opt.zero_grad()
        # Encode passages in microbatches (with grad)
        for index, passage in enumerate(pass_mbs):
            passage, mask = passage
            pass_tmp = pass_encs.copy()
            with torch.cuda.amp.autocast():
                pass_tmp[index] = self.encode_passages(
                    passage.to(self.device), mask.to(self.device)
                )
                loss, forward_acc = self.contrastive_loss(
                    torch.cat(pass_tmp), torch.cat(rev_encs)
                )
            scaler.scale(loss).backward()
        # Encode reviews in microbatches (with grad)
        for index, review in enumerate(rev_mbs):
            review, mask = review
            rev_tmp = rev_encs.copy()  # no_grad
            with torch.cuda.amp.autocast():
                rev_tmp[index] = self.encode_reviews(
                    review.to(self.device), mask.to(self.device)
                )  # grad _just_ at positions in `index`
                loss, _ = self.contrastive_loss(
                    torch.cat(pass_encs), torch.cat(rev_tmp)
                )
            scaler.scale(loss).backward()
        # Clipping
        if self.config.grad_clip != -1:
            scaler.unscale_(opt)
            torch.nn.utils.clip_grad_norm_(self.parameters(), config.grad_clip)

        scaler.step(opt)
        scaler.update()
        return {
            "Loss/Contrastive": loss,
            "Loss/Train": loss,
            "Acc/Forward": forward_acc,
        }
