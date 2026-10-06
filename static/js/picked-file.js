/*
 * Photos picked on a phone, made safe to upload -- shared by every upload box (checkout proof of payment,
 * "upload proof later", and the admin's gown / receipt / GCash QR / profile photos).
 *
 * Chrome on Android opens the system Photo Picker for a file box that only accepts image types. The file it
 * hands the page is a stand-in whose details (its modified time, sometimes its size) can change after it was
 * picked, and Chrome checks those details again while sending. When they differ it cancels the upload itself:
 * the request never reaches the server and the page only ever sees "Failed to fetch" -- which is exactly how
 * Confirm Rental failed on customers' phones (see Chromium issue 40123366).
 *
 * Three defences, all here:
 *   1. arabelaReadPickedFile copies the photo's bytes into memory the moment it is picked. That copy is what
 *      gets previewed, shrunk and sent, so the upload no longer depends on the phone's file at all.
 *   2. On Android, a photo-only file box gets one extra, made-up type in its accept list, so Chrome uses the
 *      phone's normal file chooser instead of the Photo Picker. Boxes that open the camera are left alone.
 *   3. arabelaDescribeRequestError turns the browser's raw errors into words a customer understands.
 */
(function () {
    'use strict';

    var IS_ANDROID = /Android/i.test((navigator && navigator.userAgent) || '');
    // No real file has this type. It is only there so Chrome on Android skips the Photo Picker.
    var SKIP_PHOTO_PICKER_TYPE = 'application/x-arabela-upload';
    var READ_CHUNK_BYTES = 1024 * 1024;
    var TYPES_BY_EXTENSION = { jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', webp: 'image/webp', gif: 'image/gif' };

    // Shrinking rule for proof-of-payment photos (unchanged from the checkout's original rule): a phone camera
    // photo is routinely 5-15MB, over the server's 5MB limit, while a GCash screenshot is usually under 1MB.
    var SHRINK_ABOVE_BYTES = 1.5 * 1024 * 1024;
    var SHRINK_MAX_DIMENSION = 1600;
    var SHRINK_QUALITY = 0.82;

    var UNREADABLE_MESSAGE = "We couldn't open that photo from your phone. Please choose it again "
        + '(if it keeps happening, pick it from your Files or Gallery app).';

    function unreadablePhotoError() {
        var err = new Error(UNREADABLE_MESSAGE);
        err.code = 'unreadable_photo';
        return err;
    }

    // Some pickers report no type at all; the server only accepts real image types, so fill it in from the name.
    function typeOf(file) {
        if (file.type) return file.type;
        var match = /\.([a-z0-9]+)$/i.exec(file.name || '');
        return (match && TYPES_BY_EXTENSION[match[1].toLowerCase()]) || '';
    }

    function readBytes(blob) {
        if (typeof blob.arrayBuffer === 'function') return blob.arrayBuffer();
        return new Promise(function (resolve, reject) {
            var reader = new FileReader();
            reader.onload = function () { resolve(reader.result); };
            reader.onerror = function () { reject(reader.error || new Error('read failed')); };
            reader.readAsArrayBuffer(blob);
        });
    }

    // Resolves to an in-memory copy of `file` (same name, a real image type), or rejects with an error whose
    // message can be shown as-is. Read in 1MB pieces so a large photo never needs one huge read.
    window.arabelaReadPickedFile = function (file) {
        if (!file) return Promise.reject(unreadablePhotoError());
        var size = file.size;
        var parts = [];
        var offset = 0;

        function next() {
            if (offset >= size) {
                if (!size) throw unreadablePhotoError();
                return new File(parts, file.name || 'photo.jpg', { type: typeOf(file), lastModified: Date.now() });
            }
            var end = Math.min(offset + READ_CHUNK_BYTES, size);
            return readBytes(file.slice(offset, end)).then(function (bytes) {
                if (!bytes || bytes.byteLength !== end - offset) throw unreadablePhotoError();
                parts.push(bytes);
                offset = end;
                return next();
            });
        }

        return Promise.resolve().then(next).catch(function () {
            throw unreadablePhotoError();
        });
    };

    // Shrinks a large photo; anything that goes wrong just keeps the (already in-memory) photo as it is.
    function shrinkIfLarge(file) {
        return new Promise(function (resolve) {
            if (!file || file.type.indexOf('image/') !== 0 || file.size <= SHRINK_ABOVE_BYTES) {
                resolve(file);
                return;
            }
            var objectUrl = URL.createObjectURL(file);
            var img = new Image();
            img.onload = function () {
                URL.revokeObjectURL(objectUrl);
                var scale = Math.min(1, SHRINK_MAX_DIMENSION / Math.max(img.width, img.height));
                var canvas = document.createElement('canvas');
                canvas.width = Math.max(1, Math.round(img.width * scale));
                canvas.height = Math.max(1, Math.round(img.height * scale));
                var ctx = canvas.getContext('2d');
                if (!ctx) { resolve(file); return; }
                ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
                canvas.toBlob(function (blob) {
                    if (!blob || blob.size >= file.size) { resolve(file); return; }
                    resolve(new File([blob], file.name.replace(/\.\w+$/, '') + '.jpg', { type: 'image/jpeg' }));
                }, 'image/jpeg', SHRINK_QUALITY);
            };
            img.onerror = function () {
                URL.revokeObjectURL(objectUrl);
                resolve(file);   // let the server's own type check give the real reason
            };
            img.src = objectUrl;
        });
    }

    // The proof-of-payment photo exactly as it should be sent: copied into memory, then shrunk if it is large.
    window.arabelaPreparePhotoForUpload = function (file) {
        return window.arabelaReadPickedFile(file).then(shrinkIfLarge);
    };

    // What went wrong with a fetch, in customer words. `kind` lets a page add its own context:
    //   'photo'   -- the picked photo could not be read
    //   'network' -- the request never got an answer (no connection, or the browser cancelled it)
    //   'server'  -- the server answered with something that is not our JSON (an error / busy page)
    //   'message' -- our own server's explanation (already written for the customer)
    window.arabelaDescribeRequestError = function (err, fallback) {
        var text = (err && err.message) || '';
        if (err && err.code === 'unreadable_photo') return { kind: 'photo', message: text };
        if (/Failed to fetch|NetworkError|Load failed|Network request failed|network error/i.test(text)) {
            return { kind: 'network', message: "We couldn't reach the shop's server just now. Please check your internet connection and try again." };
        }
        if (err && err.name === 'SyntaxError') {
            return { kind: 'server', message: "The shop's server is busy or didn't answer properly. Please wait a minute and try again." };
        }
        if (err && err.name === 'Error' && text) return { kind: 'message', message: text };
        return { kind: 'unknown', message: fallback || 'Something went wrong. Please try again.' };
    };

    function acceptsOnlyPhotos(input) {
        var types = (input.getAttribute('accept') || '').split(',');
        var seen = 0;
        for (var i = 0; i < types.length; i++) {
            var t = types[i].trim().toLowerCase();
            if (!t) continue;
            if (t.indexOf('image/') !== 0 && !/^\.(jpe?g|png|webp|gif)$/.test(t)) return false;
            seen++;
        }
        return seen > 0;
    }

    function keepOutOfPhotoPicker(input) {
        if (!IS_ANDROID || !input || input.type !== 'file' || input.hasAttribute('capture')) return;
        if (!acceptsOnlyPhotos(input)) return;   // already done, or not a photo-only box
        input.setAttribute('accept', input.getAttribute('accept') + ',' + SKIP_PHOTO_PICKER_TYPE);
    }

    // Before the chooser opens (it opens after the click has been handled), so boxes added later are covered too.
    document.addEventListener('click', function (event) {
        var target = event.target;
        if (target && target.tagName === 'INPUT') keepOutOfPhotoPicker(target);
    }, true);

    function prepareAll() {
        var inputs = document.querySelectorAll('input[type="file"]');
        for (var i = 0; i < inputs.length; i++) keepOutOfPhotoPicker(inputs[i]);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', prepareAll);
    } else {
        prepareAll();
    }
})();
