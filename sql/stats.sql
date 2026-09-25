-- Statistics queries for the external backend (F&S_REQUIREMENTS.md §8).
-- Connect as the read-only ``fs_stats_reader`` role. Only active records are
-- counted; archived rows are excluded from the numbers but preserved for history.

-- counts by category / product
SELECT category, product, COUNT(*) AS n
FROM records WHERE status = 'active'
GROUP BY category, product ORDER BY n DESC;

-- volume over time (monthly)
SELECT date_trunc('month', created_at) AS m, COUNT(*) AS n
FROM records WHERE status = 'active'
GROUP BY m ORDER BY m;

-- unique failures (dedup-aware)
SELECT COUNT(DISTINCT content_hash) AS unique_failures
FROM records WHERE status = 'active';

-- most common article numbers
SELECT article_number, COUNT(*) AS n
FROM records
WHERE status = 'active' AND article_number IS NOT NULL
GROUP BY article_number ORDER BY n DESC LIMIT 20;
