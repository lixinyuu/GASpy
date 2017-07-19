#!/bin/sh
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=32
#SBATCH --time=2:00:00
#SBATCH --partition=regular
#SBATCH --job-name=vasp
#SBATCH --output=enumerate-%j.out
#SBATCH --error=enumerate-%j.error
#SBATCH --constraint=haswell

module load python
cd /global/project/projectdirs/m2755/GASpy/
source activate /project/projectdirs/m2755/GASpy_conda/

PYTHONPATH='.' luigi \
    --module tasks EnumerateAlloys \
    --max-index 2 \
    --scheduler-host 128.55.144.133 \
    --workers=1 \
    --log-level=WARNING \
    --parallel-scheduling \
    --worker-timeout 300
