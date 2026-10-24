# Tests never reach the farm's real devices: a shell that sourced farm.conf / meross.env must not make a test read or
# switch the real power strip or read the real Creality printer. Tests that need these set them with mock.patch.dict.
import os

for _k in ("FARM_MEROSS_KEY", "FARM_MEROSS_DEVICES", "FARM_CREALITY_HOSTS", "FARM_PRINTGUARD_URL", "FARM_PRINTGUARD_TOKEN"):
    os.environ.pop(_k, None)
