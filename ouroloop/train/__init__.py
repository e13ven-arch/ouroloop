"""The optional training and decision stack (docs/design/SLIMMING_INVENTORY.md §0.1 A-C), off the bench path: nothing
an activity runs imports it (tests/test_hot_path_boundary.py). Its commands are `python -m ouroloop.train` (cli.py).

P1 step 4 moves the typed decision contracts, the labels and datasets, train_artifact, the backends, the stores, serving
and the trainer plugins in here, and the job infrastructure into ouroloop/train/jobs/; the v2 training chain
(docs/design/V2_TRAINING_PIPELINE.md) is written here too."""
