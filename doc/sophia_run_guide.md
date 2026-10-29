# SOPHIA: run a configuration and save the solved network

This guide uses the existing `Master_Thesis_Private` checkout and Conda/Mamba environment on SOPHIA. It starts from a new tmux session and runs the model through Slurm, stopping at the solved network instead of generating plots.

## Example paths and settings

| Item | Example |
| --- | --- |
| Repository | `/work/users/s203684/projects/Master_Thesis_Private` |
| Environment | `/work/users/s203684/projects/Master_Thesis_Private/.conda-env` |
| Main configuration | `config/config.sector_2025.yaml` |
| SOPHIA overrides | `config/config.sophia.yaml` |
| Run name | `sector_NE_june_july_2025` |
| Clusters / planning year | `10` / `2025` |
| Solved network target | `results/sector_NE_june_july_2025/networks/base_s_10___2025.nc` |
| Submission script | `run_sector_2025.sbatch` |

These values describe this example. If you change the run name, clustering, scenario options, or planning year, the target path must match the new configuration. The run folder's name is a label; the configuration's snapshot settings determine the actual simulated dates.

## 1. Connect to SOPHIA and start tmux

On your PC, open a terminal and connect (with the required DTU network/VPN access):

```bash
ssh s203684@sophia.dtu.dk
```

On SOPHIA, start a new named tmux session:

```bash
tmux new -s sector2025
```

If that session already exists, reconnect instead:

```bash
tmux ls
tmux attach -t sector2025
```

Tmux keeps your terminal session available when you disconnect. Slurm manages the actual batch run independently of tmux.

## 2. Enter the repository and check for running jobs

```bash
cd /work/users/s203684/projects/Master_Thesis_Private
pwd -P
hostname
squeue -u "$USER"
```

The directory should be the `/work/...` repository. Submit from a login node such as `sophia1`. The batch script will request a compute node automatically; a separate interactive `srun` allocation is unnecessary for this procedure.

Before updating code or configuration, make sure another active run is not using the same checkout and output paths.

## 3. Update the code if needed

If you have pushed changes to GitHub from your PC, inspect the SOPHIA checkout first:

```bash
git status
```

If there are no modified tracked files, update it:

```bash
git pull --ff-only
```

If Git reports local modifications or a conflict, resolve those before continuing. Untracked logs, the local batch script, and the environment may appear in the status output; keep them. Updating this existing checkout does not require cloning it again.

## 4. Activate the environment

```bash
mamba activate /work/users/s203684/projects/Master_Thesis_Private/.conda-env
```

You can also use `conda activate` with the same path. Check the environment:

```bash
echo "$CONDA_PREFIX"
which python
snakemake --version
```

The environment and Python should point under `/work/users/s203684/projects/Master_Thesis_Private/.conda-env`.

The existing environment is sufficient only while it remains compatible with the code's dependencies. A new software version or changed dependency specification may require an environment update.

## 5. Check the configuration and target

Use your preferred editor to save changes to:

```text
config/config.sector_2025.yaml
```

For example, you can open the SOPHIA copy from Windows File Explorer at:

```text
\\sshfs.r\s203684@sophia.dtu.dk\work\users\s203684\projects\Master_Thesis_Private\config\config.sector_2025.yaml
```

Review the run name, countries, clustering, planning year, snapshots, solver, and any settings you changed. The SOPHIA overlay is loaded after the main configuration:

```text
config/config.sophia.yaml
```

In this setup it sets the solver's temporary directory to `/scratch`. The compute node must provide that writable directory.

The example's solved network is:

```text
results/sector_NE_june_july_2025/networks/base_s_10___2025.nc
```

The `resources/.../networks/` files are intermediate networks. The solved network target is under `results/.../networks/`.

## 6. Dry-run the network target

Run this on the login node:

```bash
snakemake --dry-run --printshellcmds --cores 1 \
  results/sector_NE_june_july_2025/networks/base_s_10___2025.nc \
  --configfile config/config.sector_2025.yaml config/config.sophia.yaml
```

This checks the workflow and lists planned jobs without executing them. Targeting the network includes its required preprocessing and solve steps, but excludes downstream plots.

Normal messages include `output files have to be generated`, `input files updated by another job`, and the final `This was a dry-run` message. A dry run does not test every package, data download, or solver operation.

If you see `Nothing to be done`, Snakemake considers the target up to date. If that is unexpected after a change, inspect the configuration and rerun reasons before submitting. Do not delete results or force the entire workflow just to make it run.

## 7. Create the batch script

If the script already exists and contains the command below, reuse it. Otherwise, create or replace it using this complete block. This writes only the submission script; it does not submit a job. It overwrites any existing file with the same name.

```bash
mkdir -p logs

cat > run_sector_2025.sbatch <<'EOF'
#!/bin/bash
#SBATCH --job-name=sector2025
#SBATCH --partition=workq
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --exclusive
#SBATCH --mem=0
#SBATCH --time=48:00:00
#SBATCH --output=logs/sector2025-%j.out
#SBATCH --error=logs/sector2025-%j.err

set -euo pipefail

cd /work/users/s203684/projects/Master_Thesis_Private
source /home/s203684/miniforge3/etc/profile.d/conda.sh
conda activate "$PWD/.conda-env"

echo "Running on $(hostname)"
echo "Environment: $CONDA_PREFIX"

python -c "import gurobipy as gp; m = gp.Model(); print('Gurobi license OK'); m.dispose()"
test -w /scratch

snakemake --executor local \
  --cores "$SLURM_CPUS_PER_TASK" \
  --printshellcmds \
  results/sector_NE_june_july_2025/networks/base_s_10___2025.nc \
  --configfile config/config.sector_2025.yaml config/config.sophia.yaml
EOF
```

Paste through the final `EOF`, which must be alone on its line. If the terminal shows `>`, it is waiting for more text. Once you enter the final `EOF` and press Enter, the usual prompt returns. Avoid pressing Ctrl+C midway because that cancels creation.

Check the saved script and its shell syntax:

```bash
cat run_sector_2025.sbatch
bash -n run_sector_2025.sbatch
```

No output from `bash -n` means no shell syntax error was found; it does not validate Slurm limits or model settings.

### What the script requests

| Setting | Meaning |
| --- | --- |
| `workq` | CPU partition used in this example |
| One node, one task, 32 CPUs | One Snakemake process managing work on 32 allocated cores |
| `--exclusive` | Reserve the node for this job |
| `--mem=0` | Request all available memory on the allocated node |
| `48:00:00` | Requested maximum wall time; the job stops if it reaches the limit |
| `%j` in log names | Slurm replaces this with the job number |
| `--executor local` | Run Snakemake jobs on the compute node allocated by Slurm |

These resources follow the earlier example; adjust them to your model and current partition limits. The Gurobi test assumes the configured solver is Gurobi. If you change solvers, update that check too.

## 8. Submit: Slurm allocates the compute node

From the repository directory:

```bash
sbatch run_sector_2025.sbatch
```

An accepted submission prints something like:

```text
Submitted batch job 4909061
```

Record your new job number. Use it in all following commands instead of `JOBID`. Do not reuse a previous run's number.

The job reads the code and configuration when it runs. Leave those files unchanged while the job is pending or running.

## 9. Check the queue and follow output

```bash
squeue -u "$USER"
```

- `PD`: waiting; the last column gives the reason.
- `R`: running; the last column shows the allocated compute node.
- No rows: no active jobs; use accounting below to distinguish completion from failure.

Follow the output using your actual job number:

```bash
tail -f logs/sector2025-JOBID.out logs/sector2025-JOBID.err
```

Log files may not exist until the job starts. Normal progress messages can appear in `.err`; their presence there alone does not mean failure.

Press Ctrl+C to stop following the logs. This stops `tail`, not the batch job.

To scroll through earlier tmux output, press Ctrl+B, release both keys, then press `[`. Use Page Up or the arrows; press Q to return. On a Danish keyboard, `[` is usually AltGr+8. Alternatively, enter:

```bash
tmux copy-mode -u
```

## 10. Disconnect and return later

Detach from tmux by pressing Ctrl+B, releasing both keys, then pressing D. You can close the terminal window afterwards.

A submitted `sbatch` job continues independently of your SSH connection or tmux session. Closing an interactive allocation's shell can end that interactive allocation, so distinguish it from a submitted batch job.

Reconnect later:

```bash
ssh s203684@sophia.dtu.dk
tmux ls
tmux attach -t sector2025
```

To deliberately cancel a batch job, use its actual number:

```bash
scancel JOBID
```

## 11. Check completion and elapsed time

```bash
sacct -j JOBID --format=JobID,JobName,State,ExitCode,Start,End,Elapsed
```

Check the overall row whose JobID is exactly your number:

- `COMPLETED` and `0:0`: the batch script finished successfully.
- `FAILED`: inspect the logs for the underlying error.
- `TIMEOUT`: the allocation reached its time limit.
- `OUT_OF_MEMORY`: a memory limit was exceeded.
- `CANCELLED`: the job was cancelled.

`Elapsed` is the total batch runtime, including preprocessing and environment/solver checks. It is not just optimization time. An `extern` row marked `COMPLETED` does not establish success if the overall job is `FAILED`.

## 12. Find results and logs

### Solved network

```bash
ls -lh results/sector_NE_june_july_2025/networks/base_s_10___2025.nc
```

The `.nc` network stores the model and exported solution. A file's presence alone does not prove an optimal solve; confirm the solve log's status and termination condition as well.

### Batch logs

```bash
tail -n 100 logs/sector2025-JOBID.err
tail -n 50 logs/sector2025-JOBID.out
```

### Snakemake workflow logs

```bash
ls -lt .snakemake/log/
```

Choose the timestamp corresponding to your run. When a rule fails, Snakemake prints its specific log path; open that file to see the actual exception rather than only the workflow failure summary.

To locate solver-related logs:

```bash
find logs results/sector_NE_june_july_2025 -type f \
  \( -iname '*solve*' -o -iname '*gurobi*' \)
```

### Windows File Explorer access

Paste this into the File Explorer address bar:

```text
\\sshfs.r\s203684@sophia.dtu.dk\work\users\s203684\projects\Master_Thesis_Private
```

Open `results` for networks, `logs` for batch/rule logs, and `.snakemake/log` for workflow logs. Your original Z: connection may show `/home`; the address above opens `/work`.

## 13. Resume after fixing an error

Read the failed rule's log, fix the cause, and repeat the dry run from step 6. Then submit the script again:

```bash
sbatch run_sector_2025.sbatch
```

The new submission receives a new job number. Snakemake normally reuses completed outputs and runs the missing or outdated work. If it reports an incomplete output, review the named file and use `--rerun-incomplete` in both the dry run and batch command when appropriate.

A downstream plotting failure does not automatically invalidate an already solved network. This guide's explicit network target avoids requesting those plotting steps.

## Checklist for another configuration

1. Start or attach to tmux on SOPHIA.
2. Enter the work-directory repository and check active jobs.
3. Pull code changes if needed and activate the environment.
4. Save the main configuration and choose the correct solved-network target.
5. Update both the dry-run command and batch script with the configuration and target.
6. Review CPU, memory, time, solver, and temporary-directory settings.
7. Run the dry run and inspect the planned work.
8. Submit with `sbatch` and record the new job number.
9. Follow logs, detach, and later check accounting and solver status.

## References

- [SOPHIA job scheduler](https://dtu-sophia.github.io/docs/scheduler/)
- [SOPHIA access instructions](https://dtu-sophia.github.io/docs/access/)
- [SOPHIA file transfer](https://dtu-sophia.github.io/docs/transfer/)
- [SSHFS-Win path syntax](https://github.com/winfsp/sshfs-win#unc-syntax)
