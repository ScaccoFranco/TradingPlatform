"""Barre daily in Parquet: `python scripts/download.py [--update | --check] [--source NOME] [SIMBOLI]`."""

from quant.download import cli

if __name__ == "__main__":
    cli()
