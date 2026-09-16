import { PrismaClient } from '@prisma/client';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
const prisma = new PrismaClient();
const mediaRoot = process.env.GPOINT_MEDIA_ROOT ?? '/tmp/gpoint-media';
const fixturePng = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');
const fixtureMp4 = Buffer.from('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', 'base64');
const names = ['Luna Vale','Mira Sol','Noa Moon','Ivy North','Aria Bloom','Elle Hart','Sage West','Rue Atelier','Celia Dawn','June Still','Wren Park','Thea Coast'];
async function main(){
  await mkdir(join(mediaRoot, 'seed'), { recursive: true });
  const admin=await prisma.user.upsert({where:{telegramUserId:100000001n},update:{},create:{telegramUserId:100000001n,firstName:'Fictional Admin'}});
  const owner=await prisma.user.upsert({where:{telegramUserId:100000002n},update:{},create:{telegramUserId:100000002n,firstName:'Fictional Creator'}});
  await prisma.userRole.upsert({where:{userId_role:{userId:admin.id,role:'ADMIN'}},update:{},create:{userId:admin.id,role:'ADMIN'}});
  for(const [index,name] of names.entries()){
    const slug=name.toLowerCase().replaceAll(' ','-');
    const creator=await prisma.creator.upsert({where:{slug},update:{displayName:name},create:{displayName:name,slug}});
    await prisma.creatorProfile.upsert({where:{creatorId:creator.id},update:{status:'APPROVED',bio:'A fictional GPoint development profile.'},create:{creatorId:creator.id,status:'APPROVED',bio:'A fictional GPoint development profile.'}});
    await prisma.creatorBot.upsert({where:{telegramBotId:`seed-bot-${index}`},update:{status:'ACTIVE',miniAppEnabled:true},create:{creatorId:creator.id,telegramBotId:`seed-bot-${index}`,telegramUsername:`seed_${slug.replaceAll('-','')}_bot`,status:'ACTIVE',miniAppEnabled:true}});
    if(index===0)await prisma.creatorOwner.upsert({where:{creatorId_userId:{creatorId:creator.id,userId:owner.id}},update:{},create:{creatorId:creator.id,userId:owner.id,role:'OWNER'}});
    const offer=await prisma.vipOffer.upsert({where:{id:`seed-vip-${index}`},update:{status:'APPROVED'},create:{id:`seed-vip-${index}`,creatorId:creator.id,title:`Closer to ${name}`,description:'Fictional development offer',priceStars:69+index*10,affiliateUrl:`https://t.me/${slug.replaceAll('-','')}_bot?startapp=vip`,status:'APPROVED'}});
    for(let postIndex=0;postIndex<3;postIndex++){
      const postId=`seed-post-${index}-${postIndex}`; const assetId=`seed-media-${index}-${postIndex}`;
      const bytes=postIndex===0?fixtureMp4:fixturePng; const mimeType=postIndex===0?'video/mp4':'image/png';
      await writeFile(join(mediaRoot, 'seed', assetId), bytes);
      await prisma.mediaAsset.upsert({where:{id:assetId},update:{status:'READY',mimeType,fileSize:bytes.length},create:{id:assetId,creatorId:creator.id,kind:postIndex===0?'VIDEO':'IMAGE',storageProvider:'local',storageKey:`seed/${assetId}`,mimeType,fileSize:bytes.length,status:'READY'}});
      await prisma.post.upsert({where:{id:postId},update:{status:'PUBLISHED'},create:{id:postId,creatorId:creator.id,type:postIndex===0?'VIDEO':'IMAGE',isReel:postIndex===0,caption:`Fictional free post ${postIndex+1} by ${name}`,status:'PUBLISHED',publishedAt:new Date(Date.now()-(index*86400000+postIndex*3600000)),media:{create:{mediaAssetId:assetId,sortOrder:0}}}});
    }
    await prisma.productEvent.create({data:{creatorId:creator.id,eventType:'CREATOR_PROFILE_VIEW'}});
    if(index%2===0)await prisma.productEvent.create({data:{creatorId:creator.id,eventType:'VIP_CTA_CLICK',entityId:offer.id}});
  }
}
main().finally(()=>prisma.$disconnect());
