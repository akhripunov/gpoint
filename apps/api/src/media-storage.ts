import { randomUUID } from 'node:crypto';
import { mkdir, rm, stat, writeFile } from 'node:fs/promises';
import { join, normalize, relative } from 'node:path';
import { DeleteObjectCommand, GetObjectCommand, HeadObjectCommand, PutObjectCommand, S3Client } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
export type MediaKind = 'IMAGE' | 'VIDEO';
export interface MediaStorage { createUploadIntent(input: { kind: MediaKind; mimeType: string; fileSize: number }): Promise<{ objectKey: string; uploadUrl: string }>; finalizeUpload(objectKey: string): Promise<void>; putBytes(objectKey: string, mimeType: string, bytes: Buffer): Promise<void>; getReadUrl(objectKey: string): Promise<string>; delete(objectKey: string): Promise<void>; }
export class LocalMediaStorage implements MediaStorage { constructor(private readonly root: string) {}
  async createUploadIntent(input: { kind: MediaKind; mimeType: string; fileSize: number }) { if (!['IMAGE','VIDEO'].includes(input.kind) || input.fileSize <= 0) throw new Error('invalid media'); await mkdir(this.root,{recursive:true}); const objectKey=`${input.kind.toLowerCase()}/${randomUUID()}`; return {objectKey,uploadUrl:`/media/local/${objectKey}`}; }
  async finalizeUpload(objectKey:string){const path=this.safe(objectKey);await stat(path);}
  async putBytes(objectKey:string,_mimeType:string,bytes:Buffer){const path=this.safe(objectKey);await mkdir(join(path,'..'),{recursive:true});await writeFile(path,bytes,{flag:'wx'});}
  async getReadUrl(objectKey:string){this.safe(objectKey);return `/media/read/${encodeURIComponent(objectKey)}`;}
  async delete(objectKey:string){await rm(this.safe(objectKey),{force:true});}
  private safe(key:string){const path=normalize(join(this.root,key));if(relative(this.root,path).startsWith('..')||!relative(this.root,path))throw new Error('unsafe media path');return path;}
}
export class S3CompatibleMediaStorage implements MediaStorage { private readonly client:S3Client; constructor(private readonly bucket:string, private readonly options:{endpoint:string;region:string;accessKeyId:string;secretAccessKey:string;publicBaseUrl?:string;signedRead?:boolean}){this.client=new S3Client({endpoint:options.endpoint,region:options.region,forcePathStyle:true,credentials:{accessKeyId:options.accessKeyId,secretAccessKey:options.secretAccessKey}});} async createUploadIntent(input:{kind:MediaKind;mimeType:string;fileSize:number}){if(input.fileSize<=0)throw new Error('invalid media');const objectKey=`creators/${randomUUID()}/${input.kind.toLowerCase()}`;const command=new PutObjectCommand({Bucket:this.bucket,Key:objectKey,ContentType:input.mimeType,ContentLength:input.fileSize});return {objectKey,uploadUrl:await getSignedUrl(this.client,command,{expiresIn:900})};} async finalizeUpload(key:string){if((!key.startsWith('creators/')&&!key.startsWith('telegram/'))||key.includes('..'))throw new Error('unsafe media key');await this.client.send(new HeadObjectCommand({Bucket:this.bucket,Key:key}));} async putBytes(key:string,mimeType:string,bytes:Buffer){if((!key.startsWith('creators/')&&!key.startsWith('telegram/'))||key.includes('..'))throw new Error('unsafe media key');await this.client.send(new PutObjectCommand({Bucket:this.bucket,Key:key,ContentType:mimeType,ContentLength:bytes.length,Body:bytes}));} async getReadUrl(key:string){if((!key.startsWith('creators/')&&!key.startsWith('telegram/'))||key.includes('..'))throw new Error('unsafe media key');if(this.options.publicBaseUrl)return `${this.options.publicBaseUrl.replace(/\/$/,'')}/${key}`;if(!this.options.signedRead)throw new Error('signed read is disabled');return getSignedUrl(this.client,new GetObjectCommand({Bucket:this.bucket,Key:key}),{expiresIn:900});} async delete(key:string){if((!key.startsWith('creators/')&&!key.startsWith('telegram/'))||key.includes('..'))throw new Error('unsafe media key');await this.client.send(new DeleteObjectCommand({Bucket:this.bucket,Key:key}));} }
export function mediaStorageFromEnv(env: NodeJS.ProcessEnv = process.env): MediaStorage {
  const provider = env.MEDIA_STORAGE_PROVIDER ?? 'local';
  if (provider === 'local') return new LocalMediaStorage(env.GPOINT_MEDIA_ROOT ?? '/tmp/gpoint-media');
  if (provider !== 's3') throw new Error(`unsupported MEDIA_STORAGE_PROVIDER: ${provider}`);
  const required = ['S3_ENDPOINT', 'S3_REGION', 'S3_BUCKET', 'S3_ACCESS_KEY_ID', 'S3_SECRET_ACCESS_KEY'];
  const missing = required.filter(key => !env[key]);
  if (missing.length) throw new Error(`S3 storage configuration missing: ${missing.join(', ')}`);
  if (!env.S3_PUBLIC_BASE_URL && env.S3_SIGNED_READ !== 'true') throw new Error('S3_PUBLIC_BASE_URL or S3_SIGNED_READ=true is required');
  return new S3CompatibleMediaStorage(env.S3_BUCKET!, { endpoint: env.S3_ENDPOINT!, region: env.S3_REGION!, accessKeyId: env.S3_ACCESS_KEY_ID!, secretAccessKey: env.S3_SECRET_ACCESS_KEY!, publicBaseUrl: env.S3_PUBLIC_BASE_URL, signedRead: env.S3_SIGNED_READ === 'true' });
}
