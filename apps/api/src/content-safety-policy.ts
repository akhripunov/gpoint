export type SafetyClassification = 'SAFE' | 'SUGGESTIVE' | 'EROTIC' | 'EXPLICIT' | 'UNCERTAIN';

export type PublicationDecision = 'AUTO_PUBLISH' | 'PENDING_MODERATION' | 'BLOCKED';

/** Single policy boundary between detector output and publication/moderation. */
export function publicationDecision(classification: SafetyClassification): PublicationDecision {
  switch (classification) {
    case 'SAFE':
    case 'SUGGESTIVE':
      return 'AUTO_PUBLISH';
    case 'EXPLICIT':
      return 'BLOCKED';
    case 'EROTIC':
    case 'UNCERTAIN':
      return 'PENDING_MODERATION';
  }
}

export function manualApprovalAllowed(classification: SafetyClassification): boolean {
  return classification === 'SAFE' || classification === 'UNCERTAIN' || classification === 'SUGGESTIVE';
}

export function publicationStatus(classification: SafetyClassification, importMode: string): 'PUBLISHED' | 'PENDING' | 'PENDING_MODERATION' {
  if (classification === 'EXPLICIT') return 'PENDING_MODERATION';
  if (importMode === 'MANUAL') return 'PENDING';
  return publicationDecision(classification) === 'AUTO_PUBLISH' ? 'PUBLISHED' : 'PENDING_MODERATION';
}
