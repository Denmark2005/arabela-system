"""Photo storage used ONLY while the laptop database is in use (arabela_system/laptop_db.py).

The laptop database is a copy of the live one, so its gowns, receipts and category pictures point at the very same Cloudinary files
the live shop shows. Deleting a photo on the laptop must therefore never delete the file itself -- that would break the picture
on the live site. Uploads still work normally (they only add new files)."""
import logging

from cloudinary_storage.storage import MediaCloudinaryStorage

logger = logging.getLogger("arabela.laptop_db")


class LaptopSafeCloudinaryStorage(MediaCloudinaryStorage):
    def delete(self, name):
        logger.info("Laptop database: kept Cloudinary file %s (the live shop may still use it)", name)
