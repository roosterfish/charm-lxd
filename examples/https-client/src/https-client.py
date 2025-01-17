#!/usr/bin/env python3

"""https-client charm."""

import logging
import subprocess
from pathlib import Path

import pylxd
from ops.charm import (
    CharmBase,
    ConfigChangedEvent,
    InstallEvent,
    RelationBrokenEvent,
    RelationChangedEvent,
    RelationCreatedEvent,
    StartEvent,
)
from ops.main import main
from ops.model import ActiveStatus, Application, BlockedStatus, MaintenanceStatus, Unit

logger = logging.getLogger(__name__)

# Reduce verbosity of API calls made by pylxd
logging.getLogger("urllib3").setLevel(logging.WARNING)


class HttpsClientCharm(CharmBase):
    """https-client charm class."""

    def __init__(self, *args):
        """Initialize charm's variables."""
        super().__init__(*args)

        # Main event handlers
        self.framework.observe(self.on.config_changed, self._on_charm_config_changed)
        self.framework.observe(self.on.install, self._on_charm_install)
        self.framework.observe(self.on.start, self._on_charm_start)

        # Relation event handlers
        self.framework.observe(self.on.https_relation_broken, self._on_https_relation_broken)
        self.framework.observe(self.on.https_relation_changed, self._on_https_relation_changed)
        self.framework.observe(self.on.https_relation_created, self._on_https_relation_created)

    def generate_cert(self) -> None:
        """Generate a self-signed certificate for the client."""
        cmd = [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:secp384r1",
            "-sha384",
            "-keyout",
            "client.key",
            "-out",
            "client.crt",
            "-nodes",
            "-subj",
            f"/CN={self.unit.name.replace('/', '-')}",
            "-days",
            "3650",
        ]
        try:
            subprocess.run(cmd, capture_output=True, check=True)
        except subprocess.CalledProcessError as e:
            self.unit_blocked(f'Failed to run "{e.cmd}": {e.stderr} ({e.returncode})')
            return

    @property
    def cert(self) -> str:
        """Return the client certificate."""
        try:
            cert: str = Path("client.crt").read_text()
        except FileNotFoundError:
            return ""

        return cert

    @property
    def remote_lxd_is_clustered(self) -> bool:
        """Return True if the remote LXD is clustered, False otherwise."""
        return Path("cluster.crt").exists()

    def config_to_databag(self) -> dict:
        """Translate config data to be storable in a data bag."""
        # Prepare data to be sent (only strings, no bool nor None)
        d = {
            "version": "1.0",
            "certificate": self.cert,
        }

        projects: str = self.config.get("projects", "")
        if projects:
            d["projects"] = projects

        return d

    def _on_charm_config_changed(self, event: ConfigChangedEvent) -> None:
        """React to configuration changes.

        If the "https" relation was already established, retrigger
        the _on_https_relation_changed hook to update the remote unit
        based on our updated configuration.
        """
        logger.info("Updating charm config")
        https_relation = self.model.get_relation("https")
        if https_relation:
            # updating the unit's data bag to trigger a relation-changed on
            # the remote units
            logger.debug(f"Updating {self.unit} data bag")
            https_relation.data[self.unit].clear()
            https_relation.data[self.unit].update(self.config_to_databag())

    def _on_charm_install(self, event: InstallEvent) -> None:
        """Generate a self-signed cert if needed."""
        if self.cert:
            return

        self.unit_maintenance("Generating a self-signed cert")
        self.generate_cert()

    def _on_charm_start(self, event: StartEvent) -> None:
        """Start the unit if a cert was properly generated on install."""
        if not self.cert:
            self.unit_blocked("no cert available")
            return

        self.unit_active("Starting the https-client charm")

    def _on_https_relation_broken(self, event: RelationBrokenEvent) -> None:
        """Forget that we previously dealt with a remote LXD cluster."""
        if self.remote_lxd_is_clustered:
            Path("cluster.crt").unlink()
            logger.debug("Forgetting our previous relation with a remote LXD cluster")

    def _on_https_relation_changed(self, event: RelationChangedEvent) -> None:
        """Retrieve and display the connection information of the newly formed https relation.

        First look in the app data bag to find connection informations to a remote LXD cluster.
        Fallback to the unit data bag which is where the connection informations will be left by
        standalone LXD units.
        """
        # If we are dealing with a clustered LXD, only check connectivity
        # once for the whole cluster, not individual units
        if self.remote_lxd_is_clustered:
            if event.unit:
                remote_unit = event.unit.name
            else:
                remote_unit = "The remote unit"
            logger.debug(f"{remote_unit} is part of a known cluster, nothing to do")
            return

        version: str = ""
        remote_crt: str = "server.crt"

        for bag in (event.app, event.unit):
            if not bag:
                continue

            d = event.relation.data[bag]
            version = d.get("version", "")
            if version:
                # If the app data bag is where we found the version it
                # means we are dealing with a LXD cluster at the other end
                if bag == event.app:
                    remote_crt = "cluster.crt"
                break
            else:
                logger.debug(f"No version found in {bag.name}")

        # Help the type checker
        assert isinstance(bag, Application) or isinstance(bag, Unit)

        if not version:
            logger.error("No version found in any data bags")
            return

        if version != "1.0":
            logger.error(f"Incompatible version ({version}) found in {bag.name}")
            return

        certificate: str = d.get("certificate", "")
        certificate_fingerprint: str = d.get("certificate_fingerprint", "")
        addresses = d.get("addresses", [])

        # Convert string to list
        if addresses:
            addresses = addresses.split(",")

        logger.info(
            f"Connection information for {bag.name}:\n"
            f"certificate={certificate}\n"
            f"certificate_fingerprint={certificate_fingerprint}\n"
            f"addresses={addresses}"
        )

        if not addresses:
            logger.info(
                f"Unable to test https connectivity to {bag.name} as no address was provided"
            )
            return

        if not certificate:
            logger.info(
                f"Unable to test https connectivity to {bag.name} as no certificate was provided"
            )
            return

        # pylxd needs a CA cert on disk for verification
        with open(remote_crt, "w") as f:
            f.write(certificate)

        # Connect to the remote lxd unit
        client = pylxd.Client(
            endpoint=f"https://{addresses[0]}",
            cert=("client.crt", "client.key"),
            verify=remote_crt,
        )

        # Report remote LXD version to show the connection worked
        server_version = client.host_info["environment"]["server_version"]

        if remote_crt == "cluster.crt":
            msg = f"The cluster runs LXD version: {server_version}"
        else:
            msg = f"{bag.name} runs LXD version: {server_version}"
        logger.info(msg)

    def _on_https_relation_created(self, event: RelationCreatedEvent) -> None:
        """Upload our client certificate to the remote unit."""
        if not self.cert:
            logger.error("no cert available")
            return

        d = self.config_to_databag()
        event.relation.data[self.unit].update(d)
        # XXX: the remote unit name is still unknown at this point (_relation_created is too early)
        logger.debug("Client certificate uploaded to remote unit")

    def unit_active(self, msg: str = "") -> None:
        """Set the unit's status to active and log the provided message, if any."""
        self.unit.status = ActiveStatus()
        if msg:
            logger.debug(msg)

    def unit_blocked(self, msg: str) -> None:
        """Set the unit's status to blocked and log the provided message."""
        self.unit.status = BlockedStatus(msg)
        logger.error(msg)

    def unit_maintenance(self, msg: str) -> None:
        """Set the unit's status to maintenance and log the provided message."""
        self.unit.status = MaintenanceStatus(msg)
        logger.info(msg)


if __name__ == "__main__":
    main(HttpsClientCharm)
