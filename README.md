# LinkedIn Auto-Poster

Automatically generates and posts daily LinkedIn content using Gemini AI and RSS feeds.

---

## What you need before running

### 1. LinkedIn Access Token
- Go to [linkedin.com/developers](https://linkedin.com/developers) → Create app
- On the **Products** tab, add **Share on LinkedIn** and **Sign In with LinkedIn using OpenID Connect**
- Open the [OAuth Token Generator](https://linkedin.com/developers/tools/oauth/token-generator)
- Select your app, tick scopes: `openid`, `profile`, `w_member_social`
- Authorize and copy the token — it expires in **60 days**

### 2. Gemini API Key
- Go to [aistudio.google.com](https://aistudio.google.com) → **Get API key** → Create
- Free tier is sufficient

---

## GitHub Secrets & Variables

Go to your repo → **Settings → Secrets and variables → Actions**

### Secrets (sensitive — never visible after saving)

| Name | Required | Description |
|------|----------|-------------|
| `LINKEDIN_ACCESS_TOKEN` | Yes | Token from step 1 above |
| `GEMINI_API_KEY` | Yes | Key from step 2 above |

### Variables (visible, non-sensitive)

| Name | Required | Example | Description |
|------|----------|---------|-------------|
| `LINKEDIN_TOKEN_CREATED` | Recommended | `2026-09-26` | Date you generated the token (YYYY-MM-DD). Enables a warning at day 50 and hard stop at day 60. |
| `LINKEDIN_PERSON_URN` | Optional | `urn:li:person:AbC123` | Skip the userinfo API lookup. Leave blank and it is fetched automatically. |
| `LINKEDIN_VERSION` | Optional | `202608` | LinkedIn API version (YYYYMM). Defaults to `202608`. Update if LinkedIn retires the version. |

---

## Local setup

```bash
pip install -r requirements.txt

export GEMINI_API_KEY="your-key-here"
export LINKEDIN_ACCESS_TOKEN="your-token-here"   # only needed for a real publish

python main.py --dry-run   # prints the post, saves preview.png, publishes nothing
```

---

## config.json — what to customise

| Key | What it does |
|-----|--------------|
| `niche` | Topic area passed to Gemini as context |
| `audience` | Who the post is written for |
| `voice` | Tone and style instructions |
| `rss_feeds` | List of RSS URLs to pull news from |
| `keywords` | Items matching these are prioritised |
| `max_post_chars` | Target length the AI aims for (LinkedIn hard limit is 3000) |
| `append_source_link` | `true` adds the article URL at the end of the post |
| `image_mode` | `"card"` generates a branded image, `"none"` for text-only |
| `brand_name` | Text shown at the bottom of the image card |
| `card_bg` / `card_fg` / `card_accent` | Image card colours (hex) |

---

## Writing your own posts (queue)

Drop `.md` files into `queue/` (e.g. `001-my-take.md`). They always post before any AI-generated content and are moved to `queue/posted/` afterwards. Write a week of posts on Sunday and let the AI fill the gaps.

---

## Keeping the token alive

LinkedIn tokens expire after **60 days**.

1. Regenerate at the [OAuth Token Generator](https://linkedin.com/developers/tools/oauth/token-generator)
2. Update the `LINKEDIN_ACCESS_TOKEN` secret
3. Update the `LINKEDIN_TOKEN_CREATED` variable to today's date

Set a phone reminder every ~55 days. If you miss it, the run fails with a clear expiry message and GitHub emails you.

---

## Schedule

The workflow runs on cron `0 5 * * *` (05:00 UTC = 10:00 in UTC+5). Change the cron line in `.github/workflows/linkedin.yml` for your timezone.

To run manually: **Actions → LinkedIn daily post → Run workflow** (dry run is ticked by default).
