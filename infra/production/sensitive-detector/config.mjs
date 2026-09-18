// Runtime configuration for the pinned misskey-dev/sensitive-detector image.
// The API key is injected from the protected production env file.
export default {
  port: 3009,
  host: '0.0.0.0',
  modelDir: '/models',
  apiKey: process.env.GPOINT_SAFETY_API_KEY,
};
