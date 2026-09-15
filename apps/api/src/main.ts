import { Controller, Get, Module } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import { FastifyAdapter, NestFastifyApplication } from '@nestjs/platform-fastify';

@Controller('health') class HealthController { @Get('live') live() { return { status: 'ok' }; } }
import { ExploreController } from './explore.js';
import { AuthController, CreatorController, MeController, ReelsController, VipController } from './core-v2.js';
import { CreatorPlatformController, AdminPlatformController } from './creator-platform.js';
import { MediaReadController } from './media-read.js';
@Module({ controllers: [HealthController, ExploreController, AuthController, MeController, CreatorController, ReelsController, VipController, CreatorPlatformController, AdminPlatformController, MediaReadController] }) class AppModule {}
export async function bootstrap() { const app = await NestFactory.create<NestFastifyApplication>(AppModule, new FastifyAdapter()); app.enableCors({ origin: process.env.MINIAPP_ORIGIN?.split(',') ?? true, methods: ['GET', 'HEAD', 'POST', 'PATCH', 'DELETE', 'OPTIONS'] }); await app.listen({ port: Number(process.env.API_PORT ?? 3000), host: '0.0.0.0' }); return app; }
if (process.env.NODE_ENV !== 'test') bootstrap();
