import { Controller, Get, Module } from '@nestjs/common';
import { mediaOptimizationStatus } from './media-delivery.js';
import { NestFactory } from '@nestjs/core';
import { FastifyAdapter, NestFastifyApplication } from '@nestjs/platform-fastify';

@Controller('health') class HealthController { @Get('live') live() { return { status: 'ok' }; } @Get('ready') ready() { return { status: 'ok', imageOptimization: mediaOptimizationStatus(), imageSafety: { enabled: Boolean(process.env.GPOINT_SAFETY_URL), failClosed: true }, search: { enabled: process.env.SEARCH_ENABLED === 'true', mode: process.env.SEARCH_ENABLED === 'true' ? 'meilisearch-derived' : 'deferred' }, notifications: { enabled: process.env.NOTIFICATIONS_PROVIDER === 'novu', mode: process.env.NOTIFICATIONS_PROVIDER === 'novu' ? 'external-provider' : 'deferred' } }; } }
import { ExploreController } from './explore.js';
import { AuthController, CreatorController, MeController, VipController } from './core-v2.js';
import { PersonalizedReelsController } from './reels-discovery.js';
import { CreatorPlatformController, AdminPlatformController } from './creator-platform.js';
import { MediaReadController } from './media-read.js';
import { DiscoveryHealthController, EngagementController, FollowingController } from './discovery.js';
import { PreferencesController } from './preferences.js';
import { TelegramIngestionController } from './telegram-ingestion.js';
import { TelegramChannelController, TelegramChannelVerificationController, TelegramConnectionController } from './telegram-channel.js';
import { FeedController } from './feed.js';
import { SearchController } from './search.js';
import { DiagnosticsController } from './diagnostics.js';
import { AgeAssuranceController, MessagesController } from './messages.js';
import { AgataWorkerGatewayController } from './agata-worker-gateway.js';
@Module({ controllers: [HealthController, DiagnosticsController, SearchController, ExploreController, AuthController, MeController, CreatorController, PersonalizedReelsController, VipController, CreatorPlatformController, AdminPlatformController, MediaReadController, EngagementController, FollowingController, DiscoveryHealthController, PreferencesController, TelegramIngestionController, TelegramChannelController, TelegramChannelVerificationController, TelegramConnectionController, FeedController, MessagesController, AgeAssuranceController, AgataWorkerGatewayController] }) class AppModule {}
export async function bootstrap() { const app = await NestFactory.create<NestFastifyApplication>(AppModule, new FastifyAdapter()); app.enableCors({ origin: process.env.MINIAPP_ORIGIN?.split(',') ?? true, methods: ['GET', 'HEAD', 'POST', 'PATCH', 'DELETE', 'OPTIONS'] }); await app.listen({ port: Number(process.env.API_PORT ?? 3000), host: process.env.API_HOST ?? '0.0.0.0' }); return app; }
if (process.env.NODE_ENV !== 'test') bootstrap();
