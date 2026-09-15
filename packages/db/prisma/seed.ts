import { PrismaClient } from '@prisma/client';
const prisma = new PrismaClient();
const names = ['Luna Vale','Mira Sol','Noa Moon','Ivy North','Aria Bloom','Elle Hart','Sage West','Rue Atelier','Celia Dawn','June Still','Wren Park','Thea Coast'];
async function main(){
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
      await prisma.mediaAsset.upsert({where:{id:assetId},update:{status:'READY'},create:{id:assetId,creatorId:creator.id,kind:postIndex===0?'VIDEO':'IMAGE',storageProvider:'local',storageKey:`seed/${assetId}`,mimeType:postIndex===0?'video/mp4':'image/jpeg',fileSize:1024,status:'READY'}});
      await prisma.post.upsert({where:{id:postId},update:{status:'PUBLISHED'},create:{id:postId,creatorId:creator.id,type:postIndex===0?'VIDEO':'IMAGE',isReel:postIndex===0,caption:`Fictional free post ${postIndex+1} by ${name}`,status:'PUBLISHED',publishedAt:new Date(Date.now()-(index*86400000+postIndex*3600000)),media:{create:{mediaAssetId:assetId,sortOrder:0}}}});
    }
    await prisma.productEvent.create({data:{creatorId:creator.id,eventType:'CREATOR_PROFILE_VIEW'}});
    if(index%2===0)await prisma.productEvent.create({data:{creatorId:creator.id,eventType:'VIP_CTA_CLICK',entityId:offer.id}});
  }
}
main().finally(()=>prisma.$disconnect());
