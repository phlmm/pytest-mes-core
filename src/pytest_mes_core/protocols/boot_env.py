import anyio
import structlog
from typing import Optional
from pytest_mes_core.transports.base import DutTransport

logger = structlog.get_logger('mes_core.protocols.uboot')

class UBootShell:
    """
    Abstractions for interactive U-Boot bootloader validation.
    Assumes the framework has successfully interrupted autoboot and the DUT
    is currently idling at the U-Boot serial shell.
    Includes native asynchronous endpoints for parallel jig execution.
    """
    def __init__(self, dut: DutTransport, prompt: str = "=>"):
        self.dut = dut
        self.prompt = prompt

    def get_var(self, name: str) -> Optional[str]:
        """Reads a U-Boot environment variable using printenv."""
        res = self.dut.safe_run(f"printenv {name}", timeout_s=2.0, expected_prompt=self.prompt)
        if "not defined" in res.stdout or "Error:" in res.stdout:
            return None
        # Expected output: "name=value"
        for line in res.stdout.splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
        return None


    def set_var(self, name: str, value: str) -> None:
        """Sets a U-Boot environment variable using setenv."""
        logger.debug("setting_uboot_env_var", name=name, value=value)
        self.dut.safe_run(f"setenv {name} '{value}'", timeout_s=2.0, expected_prompt=self.prompt)


    def save_env(self) -> None:
        """Commits the current U-Boot RAM environment to persistent flash."""
        logger.info("saving_uboot_environment_to_flash")
        res = self.dut.safe_run("saveenv", timeout_s=5.0, expected_prompt=self.prompt)
        if "Failed" in res.stdout:
            raise RuntimeError(f"Failed to save U-Boot environment: {res.stdout}")


    def ping(self, ip: str) -> bool:
        """Executes a ping from the U-Boot network stack to verify Ethernet PHY connectivity."""
        logger.debug("uboot_pinging_ip", ip=ip)
        res = self.dut.safe_run(f"ping {ip}", timeout_s=5.0, expected_prompt=self.prompt)
        return "is alive" in res.stdout


    def tftp_boot(self, filename: str, load_addr: str = "${loadaddr}") -> bool:
        """Attempts to download a payload into RAM via TFTP."""
        logger.info("tftp_downloading_file", filename=filename, load_addr=load_addr)
        res = self.dut.safe_run(f"tftpboot {load_addr} {filename}", timeout_s=60.0, expected_prompt=self.prompt)
        if "Bytes transferred" in res.stdout and "TFTP error" not in res.stdout:
            return True
        return False


    def nfs_mount(self, server_ip: str, export_path: str) -> bool:
        """Mounts an NFS root file system within U-Boot by manipulating bootargs."""
        logger.info("configuring_nfs_root_bootargs", server_ip=server_ip, export_path=export_path)
        # Natively setting bootargs for NFS is usually a combination of:
        # setenv rootpath <server_ip>:<export_path>
        # setenv bootargs console=ttyS0,115200 root=/dev/nfs nfsroot=${serverip}:${rootpath},v3,tcp rw ip=dhcp
        self.set_var("rootpath", f"{server_ip}:{export_path}")
        bootargs = self.get_var("bootargs") or "console=${console}"
        
        # Inject NFS root to bootargs if not already present
        if "root=/dev/nfs" not in bootargs:
            bootargs += " root=/dev/nfs nfsroot=${serverip}:${rootpath},v3,tcp rw ip=dhcp"
            self.set_var("bootargs", bootargs)
        
        return True

