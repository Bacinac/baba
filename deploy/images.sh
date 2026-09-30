# The images BABA publishes; sourced by deploy/publish.sh and deploy/release.sh.
IMAGE_BASE="${BABA_IMAGE_BASE:-ghcr.io/bacinac/baba}"
ALL_VARIANTS=(intel nvidia cpu)

# Every service whose image we own and therefore publish, once per variant.
# Third-party images in the compose file (postgres, go2rtc, busybox) are pulled
# from their own registries by the installer and must never be pushed to ours.
SERVICES=(detector tracker api recorder event-manager ingestor doorbell
          embedder state-evaluator hwstats)

# The same image for every variant, published once: "<repo> <alias>".
COMMON_IMAGES=("web prod" "nats common")
