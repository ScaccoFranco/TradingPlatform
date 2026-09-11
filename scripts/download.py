"""Scarica barre daily in Parquet: `python scripts/download.py [--update] [SIMBOLI]`."""

from quant.download import cli

if __name__ == "__main__":
    cli()
