# Disk layout for the DataCrunch head node (KVM guest, legacy BIOS boot).
# Single 50G virtio disk /dev/vda: a 1M BIOS-boot partition for GRUB on GPT,
# then ext4 root. No ESP — the instance boots in legacy BIOS mode.
{
  disko.devices.disk.main = {
    type = "disk";
    device = "/dev/vda";
    content = {
      type = "gpt";
      partitions = {
        bios = {
          size = "1M";
          type = "EF02"; # BIOS boot partition (GRUB core.img on GPT)
        };
        root = {
          size = "100%";
          content = {
            type = "filesystem";
            format = "ext4";
            mountpoint = "/";
          };
        };
      };
    };
  };
}
