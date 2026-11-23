# TEST ONLY (SCT-1471 staging): never merge. Blocks SSH on loaders so the log collector must use the
# out-of-band channel.
from dataclasses import dataclass

from sdcm.sct_provision.user_data_objects import SctUserDataObject


@dataclass
class BlockLoaderSshUserDataObject(SctUserDataObject):
    @property
    def is_applicable(self) -> bool:
        return self.node_type == "loader"

    @property
    def script_to_run(self) -> str:
        return "systemctl mask --now ssh.socket ssh.service sshd.service || true\n"
