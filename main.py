from pathlib import Path
from finance_agent.runtime import setup
setup(Path(__file__).resolve().parent)
from simple_crawler import main
if __name__ == "__main__":
    raise SystemExit(main())
