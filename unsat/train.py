import lightning as L
from lightning.pytorch.cli import OptimizerCallable, SaveConfigCallback
from lightning.pytorch.loggers import WandbLogger
from models import UltraLocalModel
import torch
import torch.nn.functional as F
from torchmetrics.classification import Accuracy, F1Score

MODEL_CLASSES = {"ultra_local": UltraLocalModel}


class LightningTrainer(L.LightningModule):
    def __init__(
        self, model_class: str, model_kwargs: dict, optimizer: OptimizerCallable, **kwargs
    ):
        """
        Lightning module defining the model and the training loop.

        Args:
            model_class (str):
                The model class to use.
            model_kwargs (dict):
                The keyword arguments to pass to the model class.
        """
        super().__init__()
        self.optimizer = optimizer

        self.num_classes = 5
        try:
            self.model = MODEL_CLASSES[model_class](**model_kwargs, num_classes=self.num_classes)
        except KeyError:
            raise ValueError(f"Model class {model_class} not found.")

        self.metrics = torch.nn.ModuleDict()
        self.metrics['acc'] = torch.nn.ModuleDict(
            {
                'train_': Accuracy(task="multiclass", num_classes=self.num_classes),
                'val_': Accuracy(task="multiclass", num_classes=self.num_classes),
            }
        )
        self.metrics['f1'] = torch.nn.ModuleDict(
            {
                'train_': F1Score(task="multiclass", num_classes=self.num_classes),
                'val_': F1Score(task="multiclass", num_classes=self.num_classes),
            }
        )

    def training_step(self, batch, batch_idx):
        x, labels = batch  # labels shape (batch_size, X, Y)
        preds = self.model(x)  # (batch_size, C, X, Y)

        loss = self.compute_loss(preds, labels)
        self.log("train/loss", loss)

        self.compute_metrics(preds, labels, mode="train_")

        return loss

    def validation_step(self, batch, batch_idx):
        x, labels = batch
        preds = self.model(x)

        loss = self.compute_loss(preds, labels)
        self.log("val/loss", loss)

        self.compute_metrics(preds, labels, mode="val_")

    def compute_loss(self, preds, labels):
        loss = F.cross_entropy(preds, labels)
        return loss

    def compute_metrics(self, preds, labels, mode):
        self.metrics['acc'][mode](preds, labels)
        self.log(f"{mode[:-1]}/acc", self.metrics['acc'][mode], on_step=True, on_epoch=True)

        self.metrics['f1'][mode](preds, labels)
        self.log(f"{mode[:-1]}/f1", self.metrics['f1'][mode], on_step=True, on_epoch=True)

    def configure_optimizers(self):
        optimizer = self.optimizer(self.parameters())
        return optimizer


class WandbSaveConfigCallback(SaveConfigCallback):
    """
    Custom callback to save the lightning config to wandb.
    """

    def save_config(self, trainer: L.Trainer, pl_module: L.LightningModule, stage: str):
        logger = trainer.logger
        save_dir = logger.experiment.dir
        print("save dir:", save_dir)
        config_path = f"{save_dir}/{self.config_filename}"
        self.parser.save(
            self.config,
            config_path,
            skip_none=False,
            overwrite=self.overwrite,
            multifile=self.multifile,
        )
        logger.experiment.config['lightning_config'] = self.config
