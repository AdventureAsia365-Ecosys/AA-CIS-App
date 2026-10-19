export interface EndpointParam {
  name: string;
  type: string;
  required: boolean;
  example: string;
  description: string;
}

export interface Endpoint {
  id: string;
  method: "GET" | "POST" | "PATCH";
  path: string;           // may contain :id placeholder
  title: string;
  description: string;
  params: EndpointParam[];
  exampleResponse: unknown;
}

export interface EndpointGroup {
  group: string;
  icon: string;
  endpoints: Endpoint[];
}

export const ENDPOINT_GROUPS: EndpointGroup[] = [
  {
    group: "Tours", icon: "📦",
    endpoints: [
      {
        id: "list-tours",
        method: "GET",
        path: "/v1/tours/my-versions",
        title: "List My Tours",
        description: "Returns all tours in your catalog. Filter by status or country.",
        params: [
          { name: "status",    type: "string", required: false, example: "approved", description: "Filter by status (approved, ai_generated…)" },
          { name: "page_size", type: "number", required: false, example: "10",       description: "Results per page (max 50)" },
        ],
        exampleResponse: {
          data: [
            { id: "ver-uuid", version_number: 2, status: "approved", aa_name: "5-Day Cultural Vietnam",
              country: "Vietnam", duration: "5 Days", created_at: "2026-05-13T10:00:00Z" },
          ],
          pagination: { total: 15, page: 1, page_size: 10 },
        },
      },
      {
        id: "get-tour",
        method: "GET",
        path: "/v1/tours/versions/:id",
        title: "Get Tour Detail",
        description: "Full content for one tour — summary, highlights, itinerary, SEO fields.",
        params: [
          { name: "id", type: "string", required: true, example: "ver-uuid", description: "Version ID from List My Tours" },
        ],
        exampleResponse: {
          id: "ver-uuid", version_number: 2, status: "approved",
          aa_name: "5-Day Cultural Vietnam", aa_summary: "Discover the heart of Vietnam…",
          aa_highlights: "[\"Hoi An Old Town\",\"Ha Long Bay cruise\"]",
          seo_title: "5-Day Cultural Vietnam Tour | Adventure Asia",
          seo_meta: "Join our expert-led 5-day Vietnam cultural tour…",
          created_at: "2026-05-13T10:00:00Z",
        },
      },
    ],
  },
  {
    group: "Webhooks", icon: "🔔",
    endpoints: [
      {
        id: "setup-webhook",
        method: "POST",
        path: "/v1/webhooks",
        title: "Setup Webhook",
        description: "Register your endpoint to receive tour update notifications.",
        params: [
          { name: "url",    type: "string", required: true,  example: "https://yoursite.com/hook", description: "Your HTTPS endpoint URL" },
          { name: "events", type: "string", required: false, example: "tour.updated",              description: "Comma-separated event types" },
        ],
        exampleResponse: {
          webhook_id: "wh-uuid", url: "https://yoursite.com/hook",
          events: ["tour.updated", "tour.added"], status: "active",
        },
      },
    ],
  },
];
