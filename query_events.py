from pathlib import Path
from finance_agent.runtime import setup
setup(Path(__file__).resolve().parent)
from finance_agent.query import main
if __name__=="__main__":
    raise SystemExit(main())
