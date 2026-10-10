# nico-dev Enclosure: DPU image child layer on vmctl's library/dpu.
#
# Built with vmctl's `vm image build` (the only use of vmctl, at build time);
# the result is flattened to a standalone QCOW2 by build.sh and run under plain
# libvirt. Structure mirrors vmctl's library/dpu/build/Image.pkr.hcl so the
# parent variables vmctl injects (vm_parent_*, vm_ssh_*) line up.

packer {
  required_version = ">= 1.14.0"
  required_plugins {
    qemu = {
      version = "= 1.1.5"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

locals {
  vm_name = "enclosure/dpu"
  vm_from = "library/dpu:latest"
}

variable "vm_ssh_authorized_key" {
  type      = string
  sensitive = true
}

variable "vm_ssh_private_key_file" {
  type      = string
  sensitive = true
}

variable "vm_parent_url" { type = string }
variable "vm_parent_checksum" { type = string }
variable "vm_parent_virtual_size_bytes" { type = string }

variable "vm_accelerator" {
  type    = string
  default = "tcg"
  validation {
    condition     = contains(["kvm", "tcg"], var.vm_accelerator)
    error_message = "VM accelerator must be kvm or tcg."
  }
}

variable "vm_cpu_model" {
  type    = string
  default = "cortex-a72"
}

# HBN container, pinned by digest. 3.4.0-doca3.4.0 is what NICo's DPF service
# definitions pin (DOCA_HBN_SERVICE_IMAGE_TAG) and what vmctl's real-HBN
# investigation ran. Override with HBN_IMAGE through build.sh.
variable "hbn_image" {
  type    = string
  default = "nvcr.io/nvidia/doca/doca_hbn@sha256:9c2008c9ad1ad776fd1263072a78bd3dedaee2b9f315b99071d3e1495f7b1b4f"
}

source "qemu" "dpu" {
  iso_url              = var.vm_parent_url
  iso_checksum         = var.vm_parent_checksum
  disk_image           = true
  qemu_binary          = "qemu-system-aarch64"
  accelerator          = var.vm_accelerator
  machine_type         = "virt"
  cpu_model            = var.vm_cpu_model
  efi_boot             = true
  efi_firmware_code    = "/usr/share/AAVMF/AAVMF_CODE.fd"
  efi_firmware_vars    = "/usr/share/AAVMF/AAVMF_VARS.fd"
  efi_drop_efivars     = true
  qemuargs             = [["-serial", "file:/tmp/vm-enclosure-dpu-serial.log"]]
  headless             = true
  format               = "qcow2"
  use_backing_file     = true
  skip_compaction      = true
  disk_size            = "16G"
  output_directory     = "output/dpu"
  vm_name              = "dpu.qcow2"
  memory               = 4096
  cpus                 = 4
  ssh_username         = "vm"
  ssh_private_key_file = var.vm_ssh_private_key_file
  ssh_timeout          = "30m"
  shutdown_command     = "sudo shutdown -P now"
  cd_label             = "cidata"
  cd_content = {
    "meta-data" = "instance-id: vm-enclosure-dpu\nlocal-hostname: dpu\n"
    "user-data" = <<-CLOUD
      #cloud-config
      users:
        - name: vm
          groups: [adm, sudo]
          shell: /bin/bash
          sudo: ALL=(ALL) NOPASSWD:ALL
          ssh_authorized_keys:
            - ${var.vm_ssh_authorized_key}
      disable_root: true
      ssh_pwauth: false
    CLOUD
  }
}

build {
  sources = ["source.qemu.dpu"]

  # The file provisioner uploads a directory's contents only into an existing
  # destination directory; create it first.
  provisioner "shell" {
    inline = ["mkdir -p /tmp/enclosure-files"]
  }

  provisioner "file" {
    source      = "files/"
    destination = "/tmp/enclosure-files"
  }

  provisioner "shell" {
    environment_vars = ["HBN_IMAGE=${var.hbn_image}"]
    # {{ .Vars }} is where Packer injects environment_vars; without it HBN_IMAGE
    # never reaches the script.
    execute_command  = "chmod +x '{{ .Path }}'; {{ .Vars }} sudo -E '{{ .Path }}'"
    script           = "provision.sh"
  }
}

locals {
  vm_format     = "qcow2"
  vm_arch       = "aarch64"
  vm_cpus       = "4"
  vm_memory_mib = "4096"
  vm_ssh_user   = "vm"
  vm_probes     = jsonencode({ version = 1, readiness = { provider = "ssh", timeout = "30s", user = "vm", command = ["sh", "-ceu", "test \"$(uname -m)\" = aarch64; systemctl cat enclosure-hbn.service enclosure-first-boot.service >/dev/null; podman image exists \"$(cat /usr/local/lib/enclosure/hbn-image)\""] } })
}
