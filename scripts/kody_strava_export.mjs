import { createAuthenticatedFetch } from "kody:runtime";

const INPUT = null;
const PAGE_SIZE = 100;
const MAX_PAGES = 1000;

async function readStravaJson(stravaFetch, path) {
  const response = await stravaFetch(path);
  const text = await response.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    throw new Error(
      `Strava API request failed with HTTP ${response.status}: ${typeof body === "string" ? body : JSON.stringify(body)}`,
    );
  }
  return body;
}

export default async function main() {
  const stravaFetch = await createAuthenticatedFetch("strava");
  const athlete = await readStravaJson(stravaFetch, "/athlete");
  const activities = [];

  for (let page = 1; page <= MAX_PAGES; page += 1) {
    const query = new URLSearchParams({
      per_page: String(PAGE_SIZE),
      page: String(page),
    });
    if (Number.isFinite(INPUT?.after) && INPUT.after > 0) {
      query.set("after", String(Math.floor(INPUT.after)));
    }

    const batch = await readStravaJson(
      stravaFetch,
      `/athlete/activities?${query.toString()}`,
    );
    if (!Array.isArray(batch)) {
      throw new Error("Strava returned an unexpected activity list.");
    }
    activities.push(...batch);
    if (batch.length < PAGE_SIZE) {
      return { athlete, activities };
    }
  }

  throw new Error(`Strava activity export exceeded ${MAX_PAGES} pages.`);
}
