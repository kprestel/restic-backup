import logging
import os
from typing import List, Optional

from systemd import journal
import yaml
from subprocess import Popen, PIPE
import shlex
from pathlib import Path
from dataclasses import dataclass
from restic_backup.exceptions import ResticBackupException


logger = logging.getLogger(__name__)
logger.addHandler(journal.JournalHandler())

CONF_ENV_PATH = "RESTIC_BACKUP_CONF"
LOG_ENV_PATH = "RESTIC_BACKUP_LOGFILE"
# DEFAULT_CONF_PATH = "~/.restic-backup-config.yaml"
DEFAULT_CONF_PATH = "restic-backup-config.yaml"

@dataclass
class CommandResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    cmd: str


class Config:
    def __init__(self, fpath):
        try:
            with open(fpath, "r") as f:
                config = yaml.load(f, yaml.FullLoader)
        except OSError as e:
            raise ResticBackupException(f"Unable to read configuration {fpath}") from e

        try:
            self.backup = config["backup"]
        except KeyError:
            raise ResticBackupException("Missing key: backup")

        self.restic = config.get("restic_path", "/usr/bin/restic")
        self.one_file_system = self.backup.get("one_file_system", True)
        self.exclude = self.backup.get("exclude")
        self.exclude_file = self.backup.get("exclude_file")
        self.logfile = config.get("logfile", "~/restic_backup.log")

        try:
            self.email = config["email"]
        except KeyError:
            self.email_enabled = False
        else:
            self.email_enabled = self.email.get("enabled", True)


        try:
            self.directories = self.backup["directories"]
        except KeyError:
            raise ResticBackupException("Missing key: directories")
        else:
            self.directories = [Path(x) for x in self.directories]

        try:
            self.forget = config["forget"]
        except KeyError:
            self.forget_enabled = False
            logger.debug("forget is disabled due to missing key")
        else:
            self.forget_enabled = self.forget.get("enabled", True)
            self.keep = self.forget["keep"]

    def backup_cmd(self) -> str:
        cmd = (
            f"{self.restic} backup "
            f"{'--one-file-system ' if self.one_file_system else ''}"
            f"{self._list_to_cmd(self.directories)}"
        )

        if self.exclude:
            cmd += f" --exclude={self._list_to_cmd(self.exclude)}"

        if self.exclude_file:
            cmd += f" --exclude-file={self.exclude_file}"

        return cmd

    def forget_cmd(self) -> Optional[str]:
        if not self.forget_enabled:
            logger.debug("forget is not enabled")
            return None

        cmds = [f"--keep-{k} {v}" for k, v in self.keep]
        args = " ".join(cmds)
        cmd = f"{self.restic} forget {args}"

        return cmd
    def _list_to_cmd(self, l, space_at_end=False):
        ret = ""

        num_dirs = len(l)
        for i, d in enumerate(l):
            ret += f"{d}{'' if i + 1 and not space_at_end == num_dirs else ' '}"
        return ret

    def __str__(self):
        return self.backup_cmd()


def _do_backup(config: Config) -> CommandResult:
    logger.info("running backup")
    return _run_cmd(config.backup_cmd())


def _do_forget(config: Config) -> Optional[CommandResult]:
    logger.info("running forget")
    if not config.forget_enabled:
        return None

    return _run_cmd(config.forget_cmd())


def _run_cmd(cmd: str) -> CommandResult:
    cmd = shlex.split(cmd)
    logger.debug(cmd)
    proc = Popen(cmd, stderr=PIPE, stdout=PIPE)
    out, err = proc.communicate()
    return_code = proc.returncode
    return CommandResult(exit_code=return_code, stdout=out, stderr=err,
                         cmd=cmd)

def _run_check(config: Config) -> CommandResult:
    return _run_cmd(f"{config.restic} check")


def _run_main_job(config: Config) -> List[CommandResult]:
    results = []
    result = _do_backup(config)
    results.append(result)
    return_code = result.exit_code
    if return_code > 0:
        raise ResticBackupException(f"Non zero exit code: {return_code} for command: {result.cmd}")
    if config.forget_enabled:
        forget_result = _do_forget(config)

        results.append(forget_result)
        if forget_result.exit_code > 0:
            raise ResticBackupException(
                    f"Non zero exit code: {return_code} "
                    f"for command: {forget_result.cmd}")

    check_result = _run_check(config)
    results.append(check_result)
    if check_result.exit_code > 0:
        raise ResticBackupException("check failed")
    logger.info("Done")
    return results


def _send_result_email(results: List[CommandResult], config: Config):
    try:
        import yagmail
    except ImportError:
        logger.debug("yagmail not installed, not sending email")
        return

    try:
        to = config.email["to"]
    except KeyError:
        raise ResticBackupException("No email recipients configured")

    try:
        from_ = config.email["from"]
    except KeyError:
        raise ResticBackupException("Must have a 'from' address configured")

    on_success = config.email.get("on_success", True)
    on_failure = config.email.get("on_failure", True)

    contents = []
    successes = set()

    for r in results:
        was_successful = not r.exit_code <= 0
        successes.add(was_successful)
        stdout = r.stdout.decode("utf-8")
        stderr = r.stderr.decode("utf-8")
        content = (f"cmd: {r.cmd} -> {r.exit_code}\nstdout:\n{stdout}\nstderr:\n{stderr}")
        contents.append(content)

    all_successful = False in successes
    status = "was Successful" if all_successful else "Failed"

    yag = yagmail.SMTP(from_)
    if (all_successful and on_success) or (not all_successful and on_failure):
        yag.send(to=to, subject=f"Restic Backup {status}", contents="\n".join(contents))


def main():
    conf_file = os.getenv(CONF_ENV_PATH, DEFAULT_CONF_PATH)
    logger.debug(f"config_file: {conf_file}")
    config = Config(conf_file)
    results = _run_main_job(config)
    if config.email_enabled:
        _send_result_email(results, config)


if __name__ == "__main__":
    main()
