"""The training job layer (docs/TRAINING_HOSTS.md; extra `ouroloop[train-jobs]`): a trainer plugin's job on this
machine, a training host over ssh or a cloud instance, with a price cap, a deadline and cancel-on-exit.

    trainer.py      JobTrainer: the Trainer contract (ouroloop/train/train.py) as a job
    compute.py      where a job runs (this machine, or one host over ssh), poll and cancel
    worker.py       what a job runs on the training machine: `python -m ouroloop.train.jobs.worker spec.json <job id>`
    hosts/          host init and check, bootstrap.sh and start.sh (`python -m ouroloop.train host`)
    cloud.py        a RunPod or Vast.ai instance per job, always stopped
    Dockerfile      a training host as an image (the same bootstrap.sh); build from the checkout:
                    docker build -f ouroloop/train/jobs/Dockerfile -t ouroloop-worker .
"""
