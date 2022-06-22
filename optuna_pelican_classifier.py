import torch
from torch.utils.data import DataLoader

import os
import logging
import optuna
from optuna.trial import TrialState

from src.models import PELICANClassifier
from src.models import tests
from src.trainer import Trainer
from src.trainer import init_argparse, init_file_paths, init_logger, init_cuda, logging_printout, fix_args
from src.trainer import init_optimizer, init_scheduler
from src.models.metrics_classifier import metrics, minibatch_metrics, minibatch_metrics_string

from src.dataloaders import initialize_datasets, collate_fn

# This makes printing tensors more readable.
torch.set_printoptions(linewidth=1000, threshold=100000)

logger = logging.getLogger('')


def suggest_params(args, trial):

    args.lr_init = trial.suggest_loguniform("lr_init", 0.0007, 0.005)
    args.lr_final = trial.suggest_loguniform("lr_final", 1e-8, 1e-5)

    args.batch_size = trial.suggest_categorical("batch_size", [8, 10, 16, 20])

    args.config = trial.suggest_categorical("config", ["s", "S", "m", "M", "sS", "mM", "sm", "sM", "Sm", "SM", "sSm", "sSM", "smM", "sMmM", "mx", "Mx", "mxn", "mXN", "mxMX", "sXN", "smxn"])

    n_layers1 = trial.suggest_int("n_layers1", 1, 9)
    args.num_channels1 = [trial.suggest_int("n_channels1["+str(i)+"]", 10, 30) for i in range(n_layers1 + 1)]

    n_layersm = [trial.suggest_int("n_layersm["+str(i)+"]", 0, 4) for i in range(n_layers1)]
    args.num_channels_m = [[trial.suggest_int('n_channelsm['+str(i)+', '+str(k)+']', 5, 30) for k in range(n_layersm[i])] for i in range(n_layers1)]

    n_layers2 = trial.suggest_int("n_layers2", 1, 4)
    args.num_channels2 = [trial.suggest_int("n_channels2["+str(i)+"]", 5, 30) for i in range(n_layers2)]

    args.activation = trial.suggest_categorical("activation", ["relu", "elu", "leakyrelu", "silu", "selu", "tanh"])
    args.optim = trial.suggest_categorical("optim", ["adamw", "sgd", "amsgrad", "rmsprop", "adam"])

    args.activate_agg = trial.suggest_categorical("activate_agg", [True, False])
    args.activate_lin = trial.suggest_categorical("activate_lin", [True, False])
    # args.dropout = trial.suggest_categorical("dropout", [True])
    # args.batchnorm = trial.suggest_categorical("batchnorm", ['b'])

    return args

def define_model(trial):
   
    # Initialize arguments
    args = init_argparse()

    # Initialize file paths
    args = init_file_paths(args)

    # Initialize logger
    init_logger(args)

    # Suggest parameters to optuna to optimize over
    args = suggest_params(args, trial)

    # Write input paramaters and paths to log
    logging_printout(args, trial)

    # Fix possible inconsistencies in arguments
    args = fix_args(args)

    # Initialize device and data type
    device, dtype = init_cuda(args)

    # Initialize model
    model = PELICANClassifier(args.num_channels0, args.num_channels_m, args.num_channels1, args.num_channels2,
                      activate_agg=args.activate_agg, activate_lin=args.activate_lin,
                      activation=args.activation, add_beams=args.add_beams, sym=args.sym, config=args.config,
                      scale=1., ir_safe=args.ir_safe, dropout = args.dropout, batchnorm=args.batchnorm,
                      device=device, dtype=dtype)

    model.to(device)

    return args, model, device, dtype

def define_dataloader(args):

    # Initialize dataloder
    args, datasets = initialize_datasets(args, args.datadir, num_pts=None)

    # Construct PyTorch dataloaders from datasets
    collate = lambda data: collate_fn(data, scale=args.scale, nobj=args.nobj, add_beams=args.add_beams, beam_mass=args.beam_mass)
    dataloaders = {split: DataLoader(dataset,
                                     batch_size=args.batch_size,
                                     shuffle=args.shuffle if (split == 'train') else False,
                                     num_workers=args.num_workers,
                                     collate_fn=collate)
                   for split, dataset in datasets.items()}

    return args, dataloaders


def objective(trial):

    args, model, device, dtype = define_model(trial)

    args, dataloaders = define_dataloader(args)

    if args.parallel:
        model = torch.nn.DataParallel(model)

    # Initialize the scheduler and optimizer
    optimizer = init_optimizer(args, model)
    scheduler, restart_epochs, summarize = init_scheduler(args, optimizer)

    # Define a loss function.
    # loss_fn = torch.nn.functional.cross_entropy
    loss_fn = torch.nn.CrossEntropyLoss().cuda()
    
    # Apply the covariance and permutation invariance tests.
    if args.test:
        tests(model, dataloaders['train'], args, tests=['permutation','batch','irc'])

    # Instantiate the training class
    trainer = Trainer(args, dataloaders, model, loss_fn, metrics, minibatch_metrics, minibatch_metrics_string, optimizer, scheduler, restart_epochs, summarize, device, dtype)

    # Load from checkpoint file. If no checkpoint file exists, automatically does nothing.
    trainer.load_checkpoint()


    # Train model.  
    metric_to_report='accuracy'  
    trainer.train(trial=trial, metric_to_report=metric_to_report)

    best_metrics = torch.load(args.bestfile)['best_metrics']

    # # Test predictions on best model and also last checkpointed model.
    # best_loss = trainer.evaluate(splits=['test'])

    # return [best_metrics['loss'], best_metrics['accuracy'], best_metrics['AUC']]
    return best_metrics[metric_to_report]

if __name__ == '__main__':

    # Initialize arguments
    args = init_argparse()
    
    storage=f'postgresql://{os.environ["USER"]}:{args.password}@{args.host}:{args.port}'   # For running on nodes with a distributed file system
    # storage='sqlite:///file:'+args.study_name+'.db?vfs=unix-dotfile&uri=true'  # For running on a local machine

    directions = ['maximize']
    # directions=['minimize', 'maximize', 'maximize']
    sampler = optuna.samplers.TPESampler()
    pruner = optuna.pruners.HyperbandPruner()
    # pruner = optuna.pruners.MedianPruner(n_startup_trials=3, n_warmup_steps=14, n_min_trials=3)
    study = optuna.create_study(study_name=args.study_name, storage=storage, directions=directions, load_if_exists=True,
                                pruner=pruner, sampler=sampler)

    init_params =  {'activate_agg': False,
                    'activate_lin': True,
                    'activation': 'elu',
                    'batch_size': 10,
                    'config': 's',
                    'lr_final': 1e-07,
                    'lr_init': 0.001,
                    'n_channels1[0]': 3,
                    'n_channels1[1]': 15,
                    'n_channels1[2]': 15,
                    'n_channels1[3]': 15,
                    'n_channels1[4]': 15,
                    'n_channels1[5]': 15,
                    'n_channels2[0]': 30,
                    'n_channelsm[0, 0]': 15,
                    'n_channelsm[1, 0]': 15,
                    'n_channelsm[1, 1]': 15,
                    'n_channelsm[2, 0]': 15,
                    'n_channelsm[2, 1]': 15,
                    'n_channelsm[3, 0]': 15,
                    'n_channelsm[3, 1]': 15,
                    'n_channelsm[4, 0]': 15,
                    'n_channelsm[4, 1]': 15,
                    'n_layers1': 5,
                    'n_layers2': 1,
                    'n_layersm[0]': 1,
                    'n_layersm[1]': 2,
                    'n_layersm[2]': 2,
                    'n_layersm[3]': 2,
                    'n_layersm[4]': 2,
                    'optim': 'adamw',
                    }
    study.enqueue_trial(init_params)
                            
    study.optimize(objective, n_trials=30)

    pruned_trials = study.get_trials(deepcopy=False, states=[TrialState.PRUNED])
    complete_trials = study.get_trials(deepcopy=False, states=[TrialState.COMPLETE])

    print("Study statistics: ")
    print("  Number of finished trials: ", len(study.trials))
    print("  Number of pruned trials: ", len(pruned_trials))
    print("  Number of complete trials: ", len(complete_trials))

    print("Best trial:")
    trial = study.best_trial

    print("  Value: ", trial.value)

    print("  Params: ")
    for key, value in trial.params.items():
        print("    {}: {}".format(key, value))
