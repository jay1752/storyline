from dotenv import load_dotenv

# Must run before any submodule is imported — several ingestion sources read their
# API keys as module-level constants (os.environ.get(...) at import time), so env
# vars need to already be in os.environ by then, not loaded later inside a function.
load_dotenv()
