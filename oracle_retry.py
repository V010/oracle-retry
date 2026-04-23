#!/usr/bin/env python3
import time
import os
import requests
from datetime import datetime
import oci

OCI_CONFIG = {
    "user":        os.environ["OCI_USER"],
    "fingerprint": os.environ["OCI_FINGERPRINT"],
    "tenancy":     os.environ["OCI_TENANCY"],
    "region":      "eu-frankfurt-1",
    "key_content": os.environ["OCI_KEY_CONTENT"],
}

SSH_PUBLIC_KEY   = os.environ["SSH_PUBLIC_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
VCN_ID = "ocid1.vcn.oc1.eu-frankfurt-1.amaaaaaaonrfneqaccd64hf2umiyygfqvarbkas44infa7qn2rcknlijjp4q"

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception as e:
        log(f"Telegram error: {e}")

def get_or_create_subnet(network, tenancy, vcn_id):
    subnets = network.list_subnets(tenancy, vcn_id=vcn_id).data
    if subnets:
        log(f"Subnet trouvé : {subnets[0].display_name}")
        return subnets[0].id
    log("Aucun subnet — création en cours...")
    route_tables   = network.list_route_tables(tenancy, vcn_id=vcn_id).data
    security_lists = network.list_security_lists(tenancy, vcn_id=vcn_id).data
    subnet = network.create_subnet(
        oci.core.models.CreateSubnetDetails(
            compartment_id=tenancy,
            vcn_id=vcn_id,
            display_name="nico-subnet",
            cidr_block="10.0.0.0/24",
            route_table_id=route_tables[0].id if route_tables else None,
            security_list_ids=[security_lists[0].id] if security_lists else [],
        )
    ).data
    for _ in range(12):
        time.sleep(5)
        s = network.get_subnet(subnet.id).data
        if s.lifecycle_state == "AVAILABLE":
            log("Subnet disponible.")
            return subnet.id
    raise RuntimeError("Timeout : subnet non disponible.")

def main():
    log("Initialisation OCI...")
    oci.config.validate_config(OCI_CONFIG)
    compute  = oci.core.ComputeClient(OCI_CONFIG)
    network  = oci.core.VirtualNetworkClient(OCI_CONFIG)
    identity = oci.identity.IdentityClient(OCI_CONFIG)
    tenancy  = OCI_CONFIG["tenancy"]

    # Kill switch
    existing = [
        i for i in compute.list_instances(tenancy).data
        if i.lifecycle_state not in ("TERMINATED", "TERMINATING")
    ]
    if existing:
        log(f"ARRET : {len(existing)} instance(s) déjà active(s).")
        return

    ads = [ad.name for ad in identity.list_availability_domains(tenancy).data]
    log(f"ADs : {ads}")

    subnet_id = get_or_create_subnet(network, tenancy, VCN_ID)

    images = compute.list_images(
        tenancy,
        operating_system="Canonical Ubuntu",
        operating_system_version="22.04",
        shape="VM.Standard.A1.Flex",
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        log("ERREUR : image Ubuntu 22.04 ARM introuvable.")
        return
    image_id = images[0].id
    log(f"Image : {image_id[:45]}...")

    for ad in ads:
        log(f"Tentative sur {ad}...")
        try:
            instance = compute.launch_instance(
                oci.core.models.LaunchInstanceDetails(
                    display_name="nico-vm",
                    compartment_id=tenancy,
                    availability_domain=ad,
                    shape="VM.Standard.A1.Flex",
                    shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                        ocpus=4, memory_in_gbs=24,
                    ),
                    source_details=oci.core.models.InstanceSourceViaImageDetails(
                        image_id=image_id, source_type="image",
                    ),
                    create_vnic_details=oci.core.models.CreateVnicDetails(
                        subnet_id=subnet_id, assign_public_ip=True,
                    ),
                    metadata={"ssh_authorized_keys": SSH_PUBLIC_KEY},
                )
            ).data

            log("VM créée ! Attente IP...")
            for _ in range(30):
                time.sleep(10)
                attachments = compute.list_vnic_attachments(tenancy, instance_id=instance.id).data
                if attachments:
                    vnic = network.get_vnic(attachments[0].vnic_id).data
                    if vnic.public_ip:
                        msg = f"Oracle VM créée !\nIP : {vnic.public_ip}\nConnexion : ssh ubuntu@{vnic.public_ip}"
                        log(msg)
                        telegram(msg)
                        return
            telegram("Oracle VM créée mais IP pas encore assignée — vérifie la console.")
            return

        except oci.exceptions.ServiceError as e:
            if "Out of host capacity" in str(e.message):
                log(f"  Capacité indisponible sur {ad}.")
            else:
                log(f"  Erreur OCI : {e.message}")

    log("Tous les ADs saturés. Le workflow retournera dans 5 min.")

if __name__ == "__main__":
    main()
