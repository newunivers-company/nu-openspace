"""Setuptools hooks for reproducible package builds."""

from pathlib import Path
from shutil import rmtree

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


class CleanBuildPy(_build_py):
    """Remove cached package files before copying the current source tree."""

    def run(self) -> None:
        # ``build_py`` normally updates build/lib in place. Hashed dashboard
        # assets that disappear from the source tree can therefore survive and
        # leak into later wheels. Limit cleanup to this distribution's package
        # directory, then let setuptools repopulate it from current sources.
        project_root = Path(__file__).resolve().parent
        build_root = (project_root / "build").resolve()
        build_lib = Path(self.build_lib).resolve()
        if build_lib == build_root or build_root in build_lib.parents:
            package_build_dir = build_lib / "openspace"
            rmtree(package_build_dir, ignore_errors=True)
        super().run()


setup(cmdclass={"build_py": CleanBuildPy})
