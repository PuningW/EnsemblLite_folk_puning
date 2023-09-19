import os
import shutil
import sys

import pytest

from click.testing import CliRunner

from ensembl_cli.cli import download, exportrc, install


if sys.platform.startswith("linux"):
    pytest.skip("skipping cli on linux due to wakepy", allow_module_level=True)


@pytest.mark.slow
def test_download(tmp_config):
    """runs download, install, drop according to a special test cfg"""
    tmp_dir = tmp_config.parent
    # now download
    runner = CliRunner()
    r = runner.invoke(download, [f"-c{tmp_config}"], catch_exceptions=False)
    assert r.exit_code == 0, r.output
    # make sure the download checkpoint file exists
    genome_dir = tmp_dir / "staging" / "genomes"
    dirnames = [dn for dn in os.listdir(genome_dir) if (genome_dir / dn).is_dir()]
    assert "saccharomyces_cerevisiae" in dirnames

    # make sure file sizes > 0
    paths = list((genome_dir / "saccharomyces_cerevisiae").glob("*"))
    size = sum(p.stat().st_size for p in paths)
    assert size > 0

    assert r.exit_code == 0, r.output


def test_exportrc(tmp_dir):
    """exportrc works correctly"""
    runner = CliRunner()
    outdir = tmp_dir / "exported"
    r = runner.invoke(exportrc, [f"-o{outdir}"])
    assert r.exit_code == 0, r.output
    fnames = os.listdir(outdir)
    assert "species.tsv" in fnames
    assert len(fnames) == 2
    shutil.rmtree(tmp_dir)


def test_install(tmp_config):
    runner = CliRunner()
    r = runner.invoke(install, [f"-c{tmp_config}"])
