#!/bin/bash
#SBATCH --job-name=fuel_2035_w_V2G
#SBATCH --partition=workq
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --exclusive
#SBATCH --mem=0
#SBATCH --time=24:00:00
#SBATCH --array=0-224%4
#SBATCH --chdir=/work/users/s203684/projects/Master_Thesis_Private
#SBATCH --output=logs/%x-%A_%a.out
#SBATCH --error=logs/%x-%A_%a.err

# Override array ranges/concurrency with sbatch --array=START-END%LIMIT.
# Run baselines first. All disruption tasks read their completed baseline inputs.
exec bash scripts/run_disruption_sophia.sh --campaign fuel --variant 2035_w_V2G "$@"
