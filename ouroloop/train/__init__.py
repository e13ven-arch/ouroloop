"""The optional training and decision stack (docs/design/SLIMMING_INVENTORY.md §0.1 A-C; extra `ouroloop[train]`), off
the bench path: nothing an activity runs imports it (tests/test_hot_path_boundary.py). Its commands are
`python -m ouroloop.train` (cli.py).

    types, decision, policy     the typed decision contracts (noul, choice, score), decision points, routing and the
                                Clopper-Pearson threshold
    runtime, stop, views        recording and proposing decisions (the general half of the v1 runtime), StopGuard, the
                                business stop point
    feedback, dataset, checks   labels from the ledger, the task profile's training files and DataManifest, data checks
    train, calibrate            the trainer contract, recipes, train_artifact and its load probe; the temperature fit
    backends, stores, serving   how a model is asked, kept and loaded by its ref
    trainers                    the trainer plugins (tde, signature)
    config                      a config's [backends], [store], [serving] and [trainer]
    claude_code                 the Claude Code hooks adapter and its tool_gate point
    cli                         decide, smoke --jev, host
    jobs/                       the job layer (extra `ouroloop[train-jobs]`): JobTrainer, the worker a job runs, ssh
                                hosts and their bootstrap, RunPod/Vast.ai instances, the training host image

The v2 training chain (docs/design/V2_TRAINING_PIPELINE.md) is written here too.
"""
