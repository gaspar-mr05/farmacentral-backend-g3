"""Run the supervised E3 market reconciliation cycle."""

from scripts.reconcile_trades import main, run

__all__ = ["main", "run"]


if __name__ == "__main__":
    main()
