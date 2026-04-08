export function parseDomain(domain: string): { zoneName: string; subDomainName: string } {
  const parts = domain.split('.');

  if (parts.length < 2) {
    throw new Error(`Invalid domain: "${domain}"`);
  }

  if (parts.length > 3) {
    throw new Error(`Too many subdomain parts in: "${domain}"`);
  }

  if (parts.length <= 2) {
    return { zoneName: domain, subDomainName: "" };
  }

  const zoneName = parts.slice(-2).join('.');
  const subDomainName = parts.slice(0, -2).join('.');
  return { zoneName, subDomainName };
}
