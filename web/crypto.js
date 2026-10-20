// AIDEV-NOTE: mirror of src/notedump/crypto.py. Change both together.
// Blob: "NDE1" | IV (12) | AES-256-GCM ciphertext+tag, AAD = logical file name.

const MAGIC = [0x4e, 0x44, 0x45, 0x31]; // "NDE1"
const IV_LEN = 12;
const enc = new TextEncoder();

const b64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

export class DecryptError extends Error {}

/** Derive the AES key. Non-extractable: it never leaves WebCrypto. */
export async function deriveKey(passphrase, manifest) {
  const { kdf } = manifest;
  if (kdf.name !== "PBKDF2" || kdf.hash !== "SHA-256") {
    throw new DecryptError(`Unsupported key derivation: ${kdf.name}/${kdf.hash}`);
  }
  const base = await crypto.subtle.importKey(
    "raw", enc.encode(passphrase.normalize("NFC")), "PBKDF2", false, ["deriveKey"],
  );
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", hash: "SHA-256", salt: b64(kdf.salt), iterations: kdf.iterations },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["decrypt"],
  );
}

/** Decrypt one blob. Throws DecryptError on wrong key or tampering. */
export async function decrypt(key, bytes, aad) {
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  if (!MAGIC.every((b, i) => u8[i] === b)) throw new DecryptError("Not a notedump encrypted file");
  try {
    const out = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: u8.subarray(4, 4 + IV_LEN), additionalData: enc.encode(aad) },
      key,
      u8.subarray(4 + IV_LEN),
    );
    return new Uint8Array(out);
  } catch {
    throw new DecryptError("Wrong passphrase or damaged file");
  }
}

/** True when the passphrase matches the export. */
export async function verifyPassphrase(key, manifest) {
  try {
    const out = await decrypt(key, b64(manifest.check), "check");
    return new TextDecoder().decode(out) === "notedump-ok";
  } catch {
    return false;
  }
}
