import type { Endpoint } from "./endpoints-config";

const BASE = "https://api-cis.lumiguides.it.com";
const KEY  = "YOUR_API_KEY";  // placeholder — never expose real key in code sample

export type Language = "curl" | "python" | "nodejs" | "php";

export function buildCodeSample(
  endpoint: Endpoint,
  params: Record<string, string>,
  lang: Language,
): string {
  const resolvedPath = endpoint.path.replace(":id", params.id || "{tour-id}");
  const qs = endpoint.method === "GET"
    ? Object.entries(params)
        .filter(([k, v]) => k !== "id" && v)
        .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
        .join("&")
    : "";
  const url = `${BASE}${resolvedPath}${qs ? "?" + qs : ""}`;

  // POST body: all params except "id"
  const bodyParams = Object.fromEntries(
    Object.entries(params).filter(([k, v]) => k !== "id" && v)
  );
  const bodyJson = JSON.stringify(bodyParams, null, 2);

  const isPost = endpoint.method === "POST";

  const samples: Record<Language, string> = {
    curl: isPost
      ? `curl -X POST "${url}" \\
  -H "X-API-Key: ${KEY}" \\
  -H "Content-Type: application/json" \\
  -d '${JSON.stringify(bodyParams)}'`
      : `curl "${url}" \\
  -H "X-API-Key: ${KEY}"`,

    python: isPost
      ? `import requests

r = requests.post(
    "${url}",
    headers={"X-API-Key": "${KEY}"},
    json=${bodyJson}
)
print(r.json())`
      : `import requests

r = requests.get(
    "${url}",
    headers={"X-API-Key": "${KEY}"}
)
print(r.json())`,

    nodejs: isPost
      ? `const r = await fetch("${url}", {
  method: "POST",
  headers: {
    "X-API-Key": "${KEY}",
    "Content-Type": "application/json",
  },
  body: JSON.stringify(${bodyJson}),
});
const data = await r.json();`
      : `const r = await fetch("${url}", {
  headers: { "X-API-Key": "${KEY}" },
});
const data = await r.json();`,

    php: isPost
      ? `$ch = curl_init("${url}");
curl_setopt_array($ch, [
  CURLOPT_POST => true,
  CURLOPT_HTTPHEADER => ["X-API-Key: ${KEY}", "Content-Type: application/json"],
  CURLOPT_POSTFIELDS => json_encode(${bodyJson}),
  CURLOPT_RETURNTRANSFER => true,
]);
$data = json_decode(curl_exec($ch), true);`
      : `$ch = curl_init("${url}");
curl_setopt_array($ch, [
  CURLOPT_HTTPHEADER => ["X-API-Key: ${KEY}"],
  CURLOPT_RETURNTRANSFER => true,
]);
$data = json_decode(curl_exec($ch), true);`,
  };

  return samples[lang];
}
