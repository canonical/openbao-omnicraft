#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Machine abstraction for the OpenBao charm."""

import logging
import os
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import TextIO

import psutil
from charms.operator_libs_linux.v2 import snap
from openbao.openbao_managers import WorkloadBase

logger = logging.getLogger(__name__)


class Machine(WorkloadBase):
    """A class to interact with a unit machine.

    This class implements the WorkloadBase interface
    that has the same method signatures as Pebble API in the Ops
    Library.
    """

    def exists(self, path: str) -> bool:
        """Check if a file exists.

        Args:
            path: The path of the file

        Returns:
            bool: Whether the file exists
        """
        return os.path.isfile(path)

    def pull(self, path: str) -> TextIO:
        """Get the content of a file.

        Args:
            path: The path of the file

        Returns:
            str: The content of the file
        """
        return open(path, "r")

    def push(self, path: str, source: str) -> None:
        """Pushes a file to the unit.

        Args:
            path: The path of the file
            source: The contents of the file to be pushed
        """
        with open(path, "w") as write_file:
            write_file.write(source)
            logger.info("Pushed file %s", path)

    def copy_file(self, source: str, dest: str) -> None:
        """Copy a file on the unit, preserving metadata.

        Args:
            source: The path of the source file
            dest: The path of the destination file
        """
        shutil.copy2(source, dest)
        os.chmod(dest, 0o755)
        logger.info("Copied file %s to %s", source, dest)

    def make_dir(self, path: str) -> None:
        """Create a directory."""
        Path(path).mkdir(parents=True, exist_ok=True)

    def replace_directory(self, path: str) -> None:
        """Remove path if it exists and recreate it as an empty directory."""
        destination = Path(path)
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        destination.mkdir(parents=True, exist_ok=True)

    def extract_archive(self, archive: str, dest: str) -> None:
        """Extract a tar/zip archive into dest (dest must already exist)."""
        archive_path = Path(archive)
        dest_path = Path(dest)
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path) as zf:
                for info in zf.infolist():
                    member_path = Path(info.filename)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise ValueError(f"Refusing unsafe zip member path: {info.filename}")
                zf.extractall(dest_path)
        elif tarfile.is_tarfile(archive_path):
            with tarfile.open(archive_path) as tf:
                members = tf.getmembers()
                for member in members:
                    member_path = Path(member.name)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise ValueError(f"Refusing unsafe tar member path: {member.name}")
                # filter="data" is the safe default on Python 3.12+
                try:
                    tf.extractall(dest_path, members=members, filter="data")
                except TypeError:
                    tf.extractall(dest_path, members=members)
        else:
            raise ValueError(f"Unsupported hsm-lib archive format: {archive}")
        logger.info("Extracted archive %s to %s", archive, dest)

    def remove_path(self, path: str, recursive: bool = False) -> None:
        """Remove a file or directory.

        Args:
            path: The absolute path of the file or directory
            recursive: Whether to remove recursively
        raises:
            ValueError: If the path is not absolute.
        """
        if not os.path.isabs(path):
            raise ValueError(f"The provided path is not absolute: {path}")
        if os.path.isdir(path) and recursive:
            shutil.rmtree(path)
            logger.debug("Recursively removed directory `%s`", path)
        elif os.path.isfile(path) or (os.path.isdir(path) and not recursive):
            os.remove(path)
            logger.debug("Removed file or directory `%s`", path)
        else:
            raise ValueError(f"Path `{path}` does not exist.")

    def send_signal(self, signal: int, process: str) -> None:
        """Send a signal to the charm.

        Args:
            signal: The signal to send
            process: The name of the process
        """
        if pid := self._find_process(process):
            os.kill(pid, signal)
            logger.info("Sent signal %s to charm", signal)

    def restart(self, process: str) -> None:
        """Restarts all services specified in the snap."""
        snap_cache = snap.SnapCache()
        openbao_snap = snap_cache[process]
        openbao_snap.restart()

    def stop(self, process: str) -> None:
        """Stop all services of the given snap.

        Args:
            process: The name of the snap
        """
        snap_cache = snap.SnapCache()
        openbao_snap = snap_cache[process]
        openbao_snap.stop()
        logger.info("Stopped snap %s services", process)

    def get_service(self, process: str) -> psutil.Process | None:
        """Get a service.

        Args:
            process: The name of the process

        Returns:
            psutil.Process: The process
        """
        if pid := self._find_process(process):
            return psutil.Process(pid)
        return None

    def _find_process(self, process: str) -> int | None:
        """Find a process.

        Args:
            process: The name of the process

        Returns:
            int: The process ID
        """
        for proc in psutil.process_iter(attrs=["name", "pid"]):
            if proc.info["name"] == process:
                return proc.info["pid"]
        return None

    def is_accessible(self) -> bool:
        """Return True for the machine workload.

        Unlike a workload which runs in a container, the machine workload
        is always accessible, since it runs on the host machine.

        Returns:
            True
        """
        return True
