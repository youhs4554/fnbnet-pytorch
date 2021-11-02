import argparse
import os
import pathlib
import pandas as pd
import torchmetrics
import torch
from data_loader.data_loaders import VideoDataLoader
import pytorch_lightning as pl
from functools import partial
from pytorch_lightning.loggers.tensorboard import TensorBoardLogger

import model.models as model_zoo
from model.lightning_module import LitClassifier

import warnings
warnings.filterwarnings("ignore")


def prepare_dataset(data_dir, fold=1, validation=True):
    """prepare dataloaders (train/val/test) for given fold

    Args:
        data_dir (str): dataset directory path.
        fold (int, optional): fold index in [1, args.n_fold]. Defaults to 1.
        validation (bool, optional): if True, return validation dataloader. Defaults to True.

    Returns:
        dataloaders (tuple): tuple of pytorch dataloaders
    """

    # init dataset
    train_loader = VideoDataLoader(
        data_dir, batch_size=args.batch_size, fold=fold,
        sample_length=args.sample_length,
        validation_split=0.05, num_workers=args.num_workers)
    valid_loader = train_loader.split_validation()

    test_loader = VideoDataLoader(
        data_dir, batch_size=args.batch_size, fold=fold, training=False, validation_split=0.0,
        sample_length=args.sample_length,
        num_workers=args.num_workers
    )

    if validation:
        return train_loader, valid_loader, test_loader
    else:
        return train_loader, test_loader


def train_one_fold(data_dir, epochs, model_name, num_classes, base_model, lr, drop_rate,
                   tb_logger, checkpoint_callback, metrics_callbacks, fold=1):

    # fix random seed for reproducibile results
    pl.seed_everything(seed=0)

    model = getattr(model_zoo, model_name)(
        num_classes, base_model, dropout=drop_rate)
    model = LitClassifier(model)

    if model_name == "FnBNet":
        model.regularization_coeff = args.coeff

    if args.dataset == "FDD":
        pos_weight = 2.0
    elif args.dataset == "URFD":
        pos_weight = 2.0
    else:
        pos_weight = 1.0

    model.learning_rate = lr
    model.pos_weight = pos_weight
    model.metrics_callbacks = metrics_callbacks

    train_loader, valid_loader, test_loader = prepare_dataset(
        data_dir, fold=fold, validation=True,
    )
    trainer = pl.Trainer(
        gpus=None if not torch.cuda.is_available(
        ) else [int(i) for i in args.gpu_ids.split(',')],
        logger=tb_logger, callbacks=[
            checkpoint_callback],
        max_epochs=epochs, auto_lr_find=args.use_lr_finder, check_val_every_n_epoch=1,
        log_every_n_steps=10, deterministic=True)
    if args.use_lr_finder:
        trainer.tune(model, train_loader)  # find optimal lr

    # train & test
    trainer.fit(model, train_loader, valid_loader)
    test_results = trainer.test(dataloaders=test_loader)

    return test_results


def run():

    data_dir = os.path.join(args.root, args.dataset)
    output_path = pathlib.Path(args.output_path) / args.dataset

    experiment_name = "_".join(
        [args.arch, args.base_model.replace("_", ""), f"{1}x{args.sample_length}x{1}"])

    if args.arch == 'FnBNet':
        coeff_str = f"c={args.coeff:.2e}"
        experiment_name += "_" + coeff_str

    metrics_callbacks = {
        "acc": torchmetrics.functional.accuracy,
        "sens": torchmetrics.functional.recall,
        "spec": torchmetrics.functional.specificity,
        "f1": torchmetrics.functional.f1,
        "auc": partial(torchmetrics.functional.auroc, pos_label=1)
    }

    cv_results = []

    # K-fold cross-validation
    for fold in range(1, args.n_fold+1):
        # Folder hack
        tb_logger = TensorBoardLogger(
            save_dir=output_path, name=experiment_name, version=f'fold_{fold}')
        os.makedirs(output_path / experiment_name, exist_ok=True)
        checkpoint_callback = pl.callbacks.ModelCheckpoint(dirpath=tb_logger.log_dir, filename=f"epoch={{epoch:02d}}={{{args.monitor}:.4f}}",
                                                           monitor=args.monitor, mode='min' if 'loss' in args.monitor else 'max')
        cv_results.append(
            train_one_fold(data_dir, epochs=args.epochs, model_name=args.arch, num_classes=args.num_classes, base_model=args.base_model, lr=args.lr,
                           drop_rate=args.drop_rate, tb_logger=tb_logger, checkpoint_callback=checkpoint_callback, metrics_callbacks=metrics_callbacks, fold=fold)
        )

    final_result = pd.DataFrame([cv_results[i][0]
                                for i in range(args.n_fold)]).mean()

    print()
    print(f"*** Final results ({args.n_fold}-fold CV) ***\n", final_result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument('--dataset', type=str, choices=['FDD', 'URFD'])
    parser.add_argument('--root', type=str, default='/data/FallDownData')
    parser.add_argument('--output_path', type=str,
                        default='/data/FallExperiments/ckpt_dir')
    parser.add_argument('--num_classes', type=int, default=2)
    parser.add_argument('--drop_rate', type=float, default=0.8)
    parser.add_argument('--base_model', type=str, default='r2plus1d_18')
    parser.add_argument('--n_fold', type=int, default=5)
    parser.add_argument('--batch_size', type=int, default=8)
    parser.add_argument('--epochs', type=int, default=25)
    parser.add_argument('--sample_length', type=int, default=10)
    parser.add_argument('--num_workers', type=int, default=8)
    parser.add_argument('--monitor', type=str, default='val_auc')
    parser.add_argument('--use_lr_finder', default=False, action="store_true")
    parser.add_argument('--lr', type=float, default=1e-4)
    parser.add_argument('--coeff', '--c',
                        type=float, default=0.0)
    parser.add_argument('--arch', type=str, default='FnBNet',
                        choices=["FnBNet", "Baseline"])
    parser.add_argument('--gpu_ids', type=str, default='none')

    global args
    args = parser.parse_args()

    if args.gpu_ids != 'none':
        assert all(map(lambda item: item.isdigit(),
                   args.gpu_ids.split(','))), "unrecognizable gpu ids"
        if not torch.cuda.is_available():
            warnings.warn(
                f"GPU computation is ignored, since your machine is CPU-only system", ResourceWarning)

        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu_ids
    run()
