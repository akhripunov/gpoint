import { Controller, Get, Module } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import { FastifyAdapter, NestFastifyApplication } from '@nestjs/platform-fastify';

@Controller('health') class HealthController { @Get('live') live() { return { status: 'ok' }; } }
import { ExploreController } from './explore.js';
import { AuthController, CreatorController, MeController, VipController } from './core-v2.js';
import { PersonalizedReelsController } from './reels-discovery.js';
import { CreatorPlatformController, AdminPlatformController } from './creator-platform.js';
import { MediaReadController } from './media-read.js';
import { DiscoveryHealthController, EngagementController, FollowingController } from './discovery.js';
import { PreferencesController } from './preferences.js';
import { TelegramIngestionController } from './telegram-ingestion.js';
import { TelegramChannelController, TelegramChannelVerificationController } from './telegram-channel.js';
import { FeedController } from './feed.js';
@Module({ controllers: [HealthController, ExploreController, AuthController, MeController, CreatorController, PersonalizedReelsController, VipController, CreatorPlatformController, AdminPlatformController, MediaReadController, EngagementController, FollowingController, DiscoveryHealthController, PreferencesController, TelegramIngestionController, TelegramChannelController, TelegramChannelVerificationController, FeedController] }) class AppModule {}
export async function bootstrap() { const app = await NestFactory.create<NestFastifyApplication>(AppModule, new FastifyAdapter()); app.enableCors({ origin: process.env.MINIAPP_ORIGIN?.split(',') ?? true, methods: ['GET', 'HEAD', 'POST', 'PATCH', 'DELETE', 'OPTIONS'] }); await app.listen({ port: Number(process.env.API_PORT ?? 3000), host: '0.0.0.0' }); return app; }
if (process.env.NODE_ENV !== 'test') bootstrap();
