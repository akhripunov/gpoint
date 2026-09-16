export interface TelegramEntity { type?: string; offset?: number; length?: number; url?: string }

export interface SanitizedContent { text: string; removed: string[] }

const outboundLine = /(?:^|\n)\s*(?:больше|more|подпис|subscribe|пиши|write|contact|контакт|vip|вип|за зв[её]зды|stars|link|ссылка|канал|channel)\b.*$/gim;
const url = /https?:\/\/[^\s]+|t\.me\/[^\s]+/gi;
const mention = /(^|\s)@[a-zA-Z][\w]{3,}(?=\s|$|[,.!?])/g;

export class ContentSanitizer {
  sanitize(source: string | undefined, entities: TelegramEntity[] = []): SanitizedContent {
    if (!source?.trim()) return { text: '', removed: [] };
    let text = source.replace(url, '').replace(mention, '$1').replace(/(^|\s)#nogpoint\b/gi, '$1').replace(outboundLine, '');
    for (const entity of entities) {
      if (entity.type === 'text_link' || entity.type === 'url') {
        const start = Number(entity.offset); const end = start + Number(entity.length);
        if (Number.isInteger(start) && Number.isInteger(end) && start >= 0) text = text.slice(0, start) + text.slice(end);
      }
    }
    text = text.split('\n').map(line => line.trim()).filter(line => Boolean(line) && !/^(?:больше\s+(?:фото|контента)|more\s+(?:photos|content)|подпис|subscribe|пиши|write|contact|контакт|vip|вип|за зв[её]зды|stars|link|ссылка|канал|channel)(?:\s|:|$).*$/i.test(line)).join('\n').trim();
    return { text, removed: text === source.trim() ? [] : ['outbound-promotion-or-link'] };
  }
}
