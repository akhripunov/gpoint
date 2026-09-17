import sharp from 'sharp';
import { encode } from 'blurhash';

export interface ImageMetadata {
  width: number;
  height: number;
  aspectRatio: number;
  blurHash: string;
}

/** Decode a bounded preview only; originals remain immutable and are never replaced. */
export async function inspectImage(bytes: Buffer): Promise<ImageMetadata | Record<string, never>> {
  try {
    const image = sharp(bytes, { failOn: 'warning', limitInputPixels: 40_000_000 });
    const original = await image.metadata();
    const { data, info } = await image
      .rotate()
      .resize({ width: 64, height: 64, fit: 'inside', withoutEnlargement: true })
      .ensureAlpha()
      .raw()
      .toBuffer({ resolveWithObject: true });
    const previewWidth = info.width;
    const previewHeight = info.height;
    const width = original.width;
    const height = original.height;
    if (!width || !height || !previewWidth || !previewHeight || !data.length) return {};
    return { width, height, aspectRatio: width / height, blurHash: encode(new Uint8ClampedArray(data), previewWidth, previewHeight, 4, 3) };
  } catch {
    return {};
  }
}
