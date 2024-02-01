import argparse
import os
from dataclasses import dataclass, field
from typing import List, Union

# torch imports
import torch
import torch.nn.functional as F
from torch.distributed.fsdp.sharded_grad_scaler import ShardedGradScaler
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.utils.data import DataLoader

from torchtrain.profiling import maybe_run_profiler
from torchtrain.logging_utils import init_logger, rank0_log

# torchtrain related
from torchtrain.datasets import (
    create_tokenizer,
    dataloader_fn,
)
from torchtrain.models import models_config, model_name_to_cls, model_name_to_tokenizer
from torchtrain.parallelisms import ParallelDims, models_parallelize_fns
from torchtrain.lr_scheduling import get_lr_scheduler


@dataclass
class TrainState:
    step: int = 0
    current_loss: float = -1
    losses: List[float] = field(default_factory=list)


def build_optimizer(model, args):
    # build optimizer
    if args.optimizer == "Adam":
        optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    elif args.optimizer == "AdamW":
        optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    else:
        raise NotImplementedError(f"optimizer {args.optimizer} not added")

    return optimizer


def build_grad_scaler(model):
    # apply gradient scaling if mixed precision training is enabled with fp16 param dtype
    if model.mixed_precision.param_dtype == torch.float16:
        enable_grad_scaling = True
        rank0_log("Enabling gradient scaling for mixed precision training.")
    else:
        enable_grad_scaling = False
        rank0_log("Gradient scaling not enabled.")

    return ShardedGradScaler(enabled=enable_grad_scaling)


def main(args):
    init_logger()
    # init world mesh
    world_size = int(os.environ["WORLD_SIZE"])
    parallel_dims = ParallelDims(dp=args.dp_degree, sp=args.sp_degree, pp=args.pp_degree, world_size=world_size)
    world_mesh = parallel_dims.build_mesh(device_type="cuda")


    model_name = args.model
    # build tokenizer
    tokenizer_type = model_name_to_tokenizer[model_name]
    tokenizer = create_tokenizer(tokenizer_type, args.tokenizer_path)

    # build dataloader
    # need dp world size and rank
    # TODO: dp might not always be 0 so we need to handle that more carefully
    dp_degree = world_mesh.size(0)
    dp_rank = world_mesh.get_local_rank(0)
    build_dataloader_fn = dataloader_fn[args.dataset]
    data_loader = build_dataloader_fn(
        tokenizer,
        args.batch_size,
        args.seq_len,
        dp_degree,
        dp_rank,
    )

    # build model
    # TODO: add meta initialization
    model_cls = model_name_to_cls[model_name]
    model_config = models_config[model_name][args.model_conf]
    model_config.vocab_size = tokenizer.n_words

    model = model_cls.from_model_args(model_config)

    # apply PTD parallelisms + AC
    model = models_parallelize_fns[model_name](
        model,
        world_mesh,
        parallel_dims,
        args
    )

    # to use FSDP-customized gradient scaler and gradient clipping solutions
    assert isinstance(model, FSDP)

    # build optimizer after apply parallelisms to the model
    optimizer = build_optimizer(model, args)
    scheduler = get_lr_scheduler(optimizer, args)

    scaler = build_grad_scaler(model)

    # TODO: add metrics

    # torch.compile model for improved performance
    if args.compile:
        rank0_log(f"Compiling model {model_name} with torch.compile...")
        model = torch.compile(
            model,
        )

    train_state = TrainState()

    # train loop
    model.train()

    with maybe_run_profiler() as torch_profiler:
        while train_state.step < args.steps or args.steps == -1:
            train_state.step += 1
            # get batch
            batch = next(iter(data_loader))
            input_ids, labels = batch
            input_ids = input_ids.cuda()
            labels = labels.cuda()

            optimizer.zero_grad()

            # forward
            pred = model(input_ids)
            tok_loss = F.cross_entropy(
                pred.flatten(0, 1), labels.flatten(0, 1), reduction="none"
            )
            loss = tok_loss.mean()

            # backward on scaled loss to create scaled gradients
            scaler.scale(loss).backward()

            # clip gradients (after unscaling gradients of the optimizer's params)
            scaler.unscale_(optimizer)
            model.clip_grad_norm_(args.max_norm)

            # optimizer step
            # If gradients don't contain infs/NaNs, optimizer.step() is then called;
            # otherwise, optimizer.step() is skipped.
            scaler.step(optimizer)

            # updates the scale for next iteration
            scaler.update()

            # if profiler is active
            if torch_profiler:
                torch_profiler.step()

            train_state.current_loss = loss.item()
            train_state.losses.append(train_state.current_loss)

            rank0_log(
                f"step: {train_state.step}, current loss: {train_state.current_loss}, lr: {scheduler.get_last_lr()}"
            )
            scheduler.step()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TorchTrain arg parser.")
    LOCAL_WORLD_SIZE = int(os.environ["LOCAL_WORLD_SIZE"])

    parser.add_argument(
        "--model", type=str, default="llama", help="which model to train"
    )
    parser.add_argument(
        "--model_conf",
        type=str,
        default="debugmodel",
        help="which model config to train",
    )
    parser.add_argument("--dataset", type=str, default="alpaca", help="dataset to use")
    parser.add_argument(
        "--tokenizer_path",
        type=str,
        default="./torchtrain/datasets/tokenizer/tokenizer.model",
        help="tokenizer path",
    )
    parser.add_argument("--batch_size", type=int, default=8, help="batch size")
    parser.add_argument("--seq_len", type=int, default=2048, help="sequence length")
    parser.add_argument(
        "--optimizer", type=str, default="AdamW", help="optimizer to use"
    )
    parser.add_argument("--lr", type=float, default=8e-4, help="learning rate to use")
    parser.add_argument(
        "--warmup_pct",
        type=float,
        default=0.10,
        help="percentage of total training steps to use for warmup",
    )
    parser.add_argument(
        "--max_norm",
        type=Union[float, int],
        default=1.0,
        help="max norm for gradient clipping",
    )
    parser.add_argument(
        "--steps", type=int, default=-1, help="how many train steps to run"
    )
    parser.add_argument(
        "--dp_degree",
        type=int,
        default=-1,
        help="Data Parallelism degree. -1 means leftover ranks will be used (After SP/PP). 1 means disabled.",
    )
    parser.add_argument(
        "--sp_degree",
        type=int,
        default=1,
        help="Sequence Parallelism degree.  1 means disabled.",
    )
    parser.add_argument(
        "--pp_degree",
        type=int,
        default=1,
        help="Pipeline Parallelism degree (default of 1 means disabled)",
    )
    parser.add_argument(
        "--compile", action="store_true", help="Whether to compile the model."
    )

    args = parser.parse_args()
    main(args)
