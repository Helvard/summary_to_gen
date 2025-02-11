# summary_to_gen
Script der skal sende summaries til Genesys

# TO run
# Make sure you're in your project directory
cd /Users/erikhelvard/Library/CloudStorage/OneDrive-ftfa.dk/Projekter/Summary_to_gen_v2

# Run uvicorn with the virtual environment's Python
./venv_py312/bin/uvicorn main:app --reload --host 0.0.0.0 --port 8000
