-- ============================================================
-- Company Scorer — Demo Seed Data
-- Run this AFTER schema.sql in the Supabase SQL Editor.
-- ============================================================
-- Creates 3 demo companies designed to show all 3 key demo moments:
--
--   Company A — "NovaPay"
--     All must-have metrics filled, high score (~4.1/5)
--     Demonstrates: healthy scorecard with full evidence trail
--
--   Company B — "HealthStack"
--     Missing "Founders Strength" (must-have) → overall score = NULL
--     Demonstrates: incomplete scorecard + gap agent trigger
--
--   Company C — "GreenGrid"
--     All metrics filled BUT market signal is stale/low
--     Demonstrates: score drops when market agent refreshes signal
-- ============================================================


-- ============================================================
-- DATA SOURCES
-- ============================================================
INSERT INTO data_source (id, name, source_type, can_automate, cost_tier, notes) VALUES
    ('11111111-0000-0000-0000-000000000001', 'Crunchbase',        'Platform',  TRUE,  'medium', 'Good for funding rounds and company profiles'),
    ('11111111-0000-0000-0000-000000000002', 'LinkedIn',          'Platform',  FALSE, 'low',    'Primary source for team signals and headcount'),
    ('11111111-0000-0000-0000-000000000003', 'Founder Interview', 'Manual',    FALSE, 'free',   'Primary channel for qualitative data'),
    ('11111111-0000-0000-0000-000000000004', 'Pitchdeck',         'File',      FALSE, 'free',   'Inbound from founders via email or Dropbox'),
    ('11111111-0000-0000-0000-000000000005', 'Tavily',            'API',       TRUE,  'low',    'Outbound web research — automated'),
    ('11111111-0000-0000-0000-000000000006', 'Statista',          'Platform',  FALSE, 'medium', 'Market size and growth data'),
    ('11111111-0000-0000-0000-000000000007', 'Web Scrape',        'API',       TRUE,  'free',   'Automated scraping of company website'),
    ('11111111-0000-0000-0000-000000000008', 'Deal Room',         'Platform',  FALSE, 'free',   'Uploaded documents from founders')
ON CONFLICT (id) DO NOTHING;


-- ============================================================
-- METRIC TYPES  (must_have + house_weight)
-- Pulled from the Notion export:
--   must_have=true:  Competitive Landscape, Founders Strength,
--                    Technology Moat, Market Growth, Unit Economics,
--                    Financials, Company Demographics
--   must_have=false: Growth & Retention, Cash Flows, Funding Stage,
--                    Pipeline Stage, Similarity, Comparables
-- house_weight: equal weight across must-haves for now (1.0 each)
-- ============================================================
INSERT INTO metric_type (id, name, description, must_have, house_weight) VALUES
    ('22222222-0000-0000-0000-000000000001', 'Founders Strength',     'Prior exits, domain expertise, team quality',                         TRUE,  1.0),
    ('22222222-0000-0000-0000-000000000002', 'Technology Moat',       'IP, patents, data advantage, switching costs',                        TRUE,  1.0),
    ('22222222-0000-0000-0000-000000000003', 'Market Growth',         'TAM/SAM/SOM, CAGR, structural tailwinds',                             TRUE,  1.0),
    ('22222222-0000-0000-0000-000000000004', 'Competitive Landscape', 'Direct/indirect competitors, differentiation, market positioning',    TRUE,  1.0),
    ('22222222-0000-0000-0000-000000000005', 'Unit Economics',        'CAC, LTV, LTV:CAC ratio, payback period',                             TRUE,  1.0),
    ('22222222-0000-0000-0000-000000000006', 'Financials',            'Revenue, EBITDA, burn rate, runway',                                  TRUE,  1.0),
    ('22222222-0000-0000-0000-000000000007', 'Company Demographics',  'Industry, location, employee count, founding year (informational only — raw numbers not scored)', FALSE, 0.3),
    ('22222222-0000-0000-0000-000000000008', 'Growth & Retention',    'Employee growth, revenue growth, churn',                              FALSE, 0.5),
    ('22222222-0000-0000-0000-000000000009', 'Similarity Score',      'Similarity to successful portfolio companies',                        FALSE, 0.5)
ON CONFLICT (id) DO NOTHING;


-- ============================================================
-- METRICS  (individual measurable signals within each type)
-- ============================================================
INSERT INTO metric (id, metric_type_id, name, description, value_type, must_have, obtain_method, weight) VALUES

    -- Founders Strength
    ('33333333-0000-0000-0000-000000000001', '22222222-0000-0000-0000-000000000001',
        'Prior Successful Exit',   'Founder has built and exited a company before',          'boolean',   TRUE,  'Ask Founders', 1.5),
    ('33333333-0000-0000-0000-000000000002', '22222222-0000-0000-0000-000000000001',
        'Domain Expertise',        'Founder has 5+ years experience in this industry',       'score_1_5', TRUE,  'LinkedIn',     1.0),
    ('33333333-0000-0000-0000-000000000003', '22222222-0000-0000-0000-000000000001',
        'Technical Co-Founder',    'Team includes a technical co-founder',                   'boolean',   FALSE, 'LinkedIn',     0.8),

    -- Technology Moat
    ('33333333-0000-0000-0000-000000000004', '22222222-0000-0000-0000-000000000002',
        'Has Patent',              'Company holds at least one registered patent',           'boolean',   FALSE, 'Web Scrape',   1.0),
    ('33333333-0000-0000-0000-000000000005', '22222222-0000-0000-0000-000000000002',
        'Proprietary Data Moat',   'Company has unique data others cannot replicate',        'score_1_5', TRUE,  'Ask Founders', 1.2),
    ('33333333-0000-0000-0000-000000000006', '22222222-0000-0000-0000-000000000002',
        'Switching Cost',          'High effort for customers to switch to a competitor',   'score_1_5', TRUE,  'Ask Founders', 1.0),

    -- Market Growth
    ('33333333-0000-0000-0000-000000000007', '22222222-0000-0000-0000-000000000003',
        'Is in Growing Industry',  'Industry CAGR > 10% over next 3 years',                 'boolean',   TRUE,  'Tavily',       1.0),
    ('33333333-0000-0000-0000-000000000008', '22222222-0000-0000-0000-000000000003',
        'Is in Growing Region',    'VC activity and startup ecosystem growing in HQ country','boolean',   TRUE,  'Tavily',       1.0),
    ('33333333-0000-0000-0000-000000000009', '22222222-0000-0000-0000-000000000003',
        'Market Size Score',       'TAM > $1B = 5, $500M–$1B = 4, <$500M = 3 or below',    'score_1_5', TRUE,  'Statista',     1.2),

    -- Competitive Landscape
    ('33333333-0000-0000-0000-000000000010', '22222222-0000-0000-0000-000000000004',
        'Has High Competition',    'Many well-funded direct competitors exist',              'boolean',   TRUE,  'Tavily',       1.0),
    ('33333333-0000-0000-0000-000000000011', '22222222-0000-0000-0000-000000000004',
        'Clear Differentiation',   'Product is clearly differentiated from competitors',    'score_1_5', TRUE,  'Ask Founders', 1.2),

    -- Unit Economics
    ('33333333-0000-0000-0000-000000000012', '22222222-0000-0000-0000-000000000005',
        'LTV:CAC Ratio',           'LTV divided by CAC (>3 is good, >5 is excellent)',      'score_1_5', TRUE,  'Ask Founders', 1.5),
    ('33333333-0000-0000-0000-000000000013', '22222222-0000-0000-0000-000000000005',
        'Payback Period (months)', 'Months to recover CAC (lower = better)',                'number',    FALSE, 'Ask Founders', 1.0),

    -- Financials
    ('33333333-0000-0000-0000-000000000014', '22222222-0000-0000-0000-000000000006',
        'Monthly Recurring Revenue','MRR in USD',                                           'number',    FALSE, 'Ask Founders', 1.0),
    ('33333333-0000-0000-0000-000000000015', '22222222-0000-0000-0000-000000000006',
        'Funding Stage',           'Current funding stage (encoded as score)',               'score_1_5', TRUE,  'Crunchbase',   1.0),
    ('33333333-0000-0000-0000-000000000016', '22222222-0000-0000-0000-000000000006',
        'Runway (months)',         'Months of runway remaining',                             'number',    FALSE, 'Ask Founders', 0.8),

    -- Company Demographics
    ('33333333-0000-0000-0000-000000000017', '22222222-0000-0000-0000-000000000007',
        'Employee Count',          'Number of full-time employees',                          'number',    FALSE, 'LinkedIn',     0.5),
    ('33333333-0000-0000-0000-000000000018', '22222222-0000-0000-0000-000000000007',
        'Founding Year',           'Year the company was founded',                           'number',    FALSE, 'Crunchbase',   0.3),

    -- Growth & Retention
    ('33333333-0000-0000-0000-000000000019', '22222222-0000-0000-0000-000000000008',
        'Headcount Growth 6m',     '% headcount growth over last 6 months',                 'number',    FALSE, 'LinkedIn',     1.0),

    -- Similarity
    ('33333333-0000-0000-0000-000000000020', '22222222-0000-0000-0000-000000000009',
        'Similarity to Portfolio', 'LLM-scored similarity to successful portfolio companies','score_1_5', FALSE, 'LLM',          1.0)

ON CONFLICT (id) DO NOTHING;


-- ============================================================
-- COMPANIES
-- ============================================================
INSERT INTO company (id, name, website, country, industry, pipeline_stage, source_type, source_channel, notes) VALUES
    ('44444444-0000-0000-0000-000000000001',
        'NovaPay', 'https://novapay.io', 'UAE', 'Fintech',
        'due_diligence', 'inbound', 'Email',
        'B2B payments infrastructure for MENA SMEs. Strong team, Series A. All metrics filled.'),
    ('44444444-0000-0000-0000-000000000002',
        'HealthStack', 'https://healthstack.ai', 'Egypt', 'HealthTech',
        'deal_sourcing', 'inbound', 'Dropbox',
        'AI-powered clinical decision support. Founders Strength data missing — gap agent demo.'),
    ('44444444-0000-0000-0000-000000000003',
        'GreenGrid', 'https://greengrid.io', 'Saudi Arabia', 'CleanTech',
        'first_contact', 'outbound', 'LinkedIn',
        'Grid-scale battery storage. Market signal is stale — market agent refresh demo.')
ON CONFLICT (id) DO NOTHING;

-- Company LinkedIn URLs (added after initial insert so existing DBs get updated)
UPDATE company SET linkedin_url = 'https://www.linkedin.com/company/novapay-mena'     WHERE id = '44444444-0000-0000-0000-000000000001';
UPDATE company SET linkedin_url = 'https://www.linkedin.com/company/healthstack-ai'   WHERE id = '44444444-0000-0000-0000-000000000002';
UPDATE company SET linkedin_url = 'https://www.linkedin.com/company/greengrid-energy' WHERE id = '44444444-0000-0000-0000-000000000003';


-- ============================================================
-- FOUNDERS  (2 per company, with LinkedIn profiles)
-- Fictional profiles for the demo — real product should be fed from
-- deck parsing or manual entry.
-- ============================================================
INSERT INTO founder (company_id, name, title, linkedin_url) VALUES
    -- NovaPay
    ('44444444-0000-0000-0000-000000000001', 'Ahmed Al-Rashid', 'CEO & Co-founder',
     'https://www.linkedin.com/in/ahmed-al-rashid-novapay'),
    ('44444444-0000-0000-0000-000000000001', 'Sara Khoury',     'CTO & Co-founder',
     'https://www.linkedin.com/in/sara-khoury-novapay'),

    -- HealthStack
    ('44444444-0000-0000-0000-000000000002', 'Dr. Mona El-Sayed', 'CEO & Co-founder',
     'https://www.linkedin.com/in/mona-elsayed-healthstack'),
    ('44444444-0000-0000-0000-000000000002', 'Youssef Hassan',    'CTO & Co-founder',
     'https://www.linkedin.com/in/youssef-hassan-healthstack'),

    -- GreenGrid
    ('44444444-0000-0000-0000-000000000003', 'Fahad Al-Saud',   'CEO & Co-founder',
     'https://www.linkedin.com/in/fahad-alsaud-greengrid'),
    ('44444444-0000-0000-0000-000000000003', 'Layla Al-Harbi',  'COO & Co-founder',
     'https://www.linkedin.com/in/layla-alharbi-greengrid')
ON CONFLICT DO NOTHING;


-- ============================================================
-- HOUSE WEIGHTS  (equal weight across all metric types for now)
-- ============================================================
INSERT INTO user_weight (user_id, metric_type_id, weight) VALUES
    ('house', '22222222-0000-0000-0000-000000000001', 1.0),  -- Founders Strength
    ('house', '22222222-0000-0000-0000-000000000002', 1.0),  -- Technology Moat
    ('house', '22222222-0000-0000-0000-000000000003', 1.0),  -- Market Growth
    ('house', '22222222-0000-0000-0000-000000000004', 1.0),  -- Competitive Landscape
    ('house', '22222222-0000-0000-0000-000000000005', 1.0),  -- Unit Economics
    ('house', '22222222-0000-0000-0000-000000000006', 1.0),  -- Financials
    ('house', '22222222-0000-0000-0000-000000000007', 1.0),  -- Company Demographics
    ('house', '22222222-0000-0000-0000-000000000008', 0.5),  -- Growth & Retention (nice-to-have)
    ('house', '22222222-0000-0000-0000-000000000009', 0.5)   -- Similarity Score (nice-to-have)
ON CONFLICT (user_id, metric_type_id) DO NOTHING;


-- ============================================================
-- COMPANY METRIC VALUES
-- ============================================================

-- ----------------------------------------------------------
-- COMPANY A: NovaPay — all must-haves filled, strong scores
-- ----------------------------------------------------------

-- Founders Strength
INSERT INTO company_metric_value (company_id, metric_id, value, source_id, raw_evidence, captured_by, confidence, is_latest) VALUES
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000001',
        'true', '11111111-0000-0000-0000-000000000003',
        'Founder Ahmed Al-Rashid previously co-founded PayLink (acquired by Mastercard, 2021). Confirmed in founder interview Jan 2025.',
        'analyst_alex', 0.95, TRUE),
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000002',
        '4', '11111111-0000-0000-0000-000000000002',
        'LinkedIn: Ahmed Al-Rashid — 8 years at SWIFT, 3 years at Stripe MENA. Strong fintech pedigree.',
        'analyst_alex', 0.90, TRUE),
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000003',
        'true', '11111111-0000-0000-0000-000000000002',
        'LinkedIn: CTO Sara Chen — 6 years at Adyen, contributed to open-source payment SDKs.',
        'analyst_alex', 0.85, TRUE),

-- Technology Moat
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000004',
        'false', '11111111-0000-0000-0000-000000000007',
        'No patent found on UAE patent registry or company website as of Feb 2025.',
        'analyst_alex', 0.80, TRUE),
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000005',
        '4', '11111111-0000-0000-0000-000000000003',
        'Founder interview: "We have 3 years of anonymised SME transaction data — 2.1M transactions — that took us 2 years to clean and model."',
        'analyst_alex', 0.92, TRUE),
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000006',
        '4', '11111111-0000-0000-0000-000000000004',
        'Pitchdeck slide 9: "Average integration depth is 14 API endpoints. Switching cost estimated at 3 engineering months."',
        'analyst_alex', 0.88, TRUE),

-- Market Growth
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000007',
        'true', '11111111-0000-0000-0000-000000000006',
        'Statista 2024: MENA digital payments market CAGR 17.3% through 2028. SME segment growing faster at 22%.',
        'analyst_alex', 0.90, TRUE),
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000008',
        'true', '11111111-0000-0000-0000-000000000005',
        'Tavily search Feb 2025: UAE VC investment up 34% YoY. DIFC fintech hub now 3rd largest in world.',
        'market_agent', 0.85, TRUE),
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000009',
        '5', '11111111-0000-0000-0000-000000000006',
        'Statista: MENA B2B payments TAM $320B by 2027. SME addressable market $45B.',
        'analyst_alex', 0.88, TRUE),

-- Competitive Landscape
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000010',
        'true', '11111111-0000-0000-0000-000000000005',
        'Tavily: Identified 6 direct competitors — Paymennt, Telr, PayTabs, Checkout.com, Stripe (limited MENA), Paymob.',
        'analyst_alex', 0.85, TRUE),
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000011',
        '4', '11111111-0000-0000-0000-000000000004',
        'Pitchdeck slide 12: NovaPay is the only provider with native Arabic-language reconciliation and VAT-compliant reporting built in.',
        'analyst_alex', 0.87, TRUE),

-- Unit Economics
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000012',
        '4', '11111111-0000-0000-0000-000000000003',
        'Founder interview: LTV:CAC of 4.2x at current cohort. CAC $1,200, LTV $5,040 over 42-month average tenure.',
        'analyst_alex', 0.92, TRUE),
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000013',
        '14', '11111111-0000-0000-0000-000000000003',
        'Founder interview: "We recover CAC in 14 months on average, down from 22 months a year ago."',
        'analyst_alex', 0.90, TRUE),

-- Financials
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000014',
        '185000', '11111111-0000-0000-0000-000000000003',
        'Founder interview: $185K MRR as of January 2025, up from $90K in July 2024.',
        'analyst_alex', 0.95, TRUE),
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000015',
        '3', '11111111-0000-0000-0000-000000000001',
        'Crunchbase: Series A — $8M raised, led by MENA Ventures and Global Founders Capital, closed Nov 2024.',
        'analyst_alex', 0.98, TRUE),
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000016',
        '22', '11111111-0000-0000-0000-000000000003',
        'Founder interview: 22 months runway at current burn of $380K/month.',
        'analyst_alex', 0.92, TRUE),

-- Company Demographics
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000017',
        '34', '11111111-0000-0000-0000-000000000002',
        'LinkedIn company page: 34 employees as of February 2025.',
        'analyst_alex', 0.90, TRUE),
    ('44444444-0000-0000-0000-000000000001', '33333333-0000-0000-0000-000000000018',
        '2022', '11111111-0000-0000-0000-000000000001',
        'Crunchbase: Founded 2022, incorporated in DIFC, Dubai.',
        'analyst_alex', 0.99, TRUE);


-- ----------------------------------------------------------
-- COMPANY B: HealthStack — Founders Strength MISSING (gap demo)
-- Must-have metric type has no value → overall_score = NULL
-- ----------------------------------------------------------

-- Technology Moat (filled)
INSERT INTO company_metric_value (company_id, metric_id, value, source_id, raw_evidence, captured_by, confidence, is_latest) VALUES
    ('44444444-0000-0000-0000-000000000002', '33333333-0000-0000-0000-000000000005',
        '3', '11111111-0000-0000-0000-000000000004',
        'Pitchdeck: "Proprietary clinical NLP model trained on 800K Arabic-language medical records from 3 partner hospitals."',
        'analyst_alex', 0.82, TRUE),
    ('44444444-0000-0000-0000-000000000002', '33333333-0000-0000-0000-000000000006',
        '3', '11111111-0000-0000-0000-000000000004',
        'Pitchdeck: Integrated into hospital EHR systems — switching requires re-training clinical staff.',
        'analyst_alex', 0.75, TRUE),

-- Market Growth (filled)
    ('44444444-0000-0000-0000-000000000002', '33333333-0000-0000-0000-000000000007',
        'true', '11111111-0000-0000-0000-000000000005',
        'Tavily: MENA digital health market growing at 21% CAGR through 2027 (CB Insights, 2024).',
        'market_agent', 0.88, TRUE),
    ('44444444-0000-0000-0000-000000000002', '33333333-0000-0000-0000-000000000008',
        'true', '11111111-0000-0000-0000-000000000005',
        'Tavily: Egypt HealthTech ecosystem saw $120M invested in 2024, up 45% from 2023.',
        'market_agent', 0.82, TRUE),
    ('44444444-0000-0000-0000-000000000002', '33333333-0000-0000-0000-000000000009',
        '4', '11111111-0000-0000-0000-000000000006',
        'Statista: MENA digital health TAM $15B by 2028.',
        'analyst_alex', 0.85, TRUE),

-- Competitive Landscape (filled)
    ('44444444-0000-0000-0000-000000000002', '33333333-0000-0000-0000-000000000010',
        'false', '11111111-0000-0000-0000-000000000005',
        'Tavily: Limited direct competitors in Arabic-language clinical AI. Main alternatives are paper-based or generic Western LLMs.',
        'analyst_alex', 0.80, TRUE),
    ('44444444-0000-0000-0000-000000000002', '33333333-0000-0000-0000-000000000011',
        '4', '11111111-0000-0000-0000-000000000004',
        'Pitchdeck: Only Arabic-native clinical NLP with HIPAA + Saudi PDPL compliance built in.',
        'analyst_alex', 0.83, TRUE),

-- Financials (partially filled)
    ('44444444-0000-0000-0000-000000000002', '33333333-0000-0000-0000-000000000015',
        '2', '11111111-0000-0000-0000-000000000001',
        'Crunchbase: Seed stage — $1.5M raised from 500 Global and Flat6Labs, closed Mar 2024.',
        'analyst_alex', 0.95, TRUE),

-- Company Demographics (filled)
    ('44444444-0000-0000-0000-000000000002', '33333333-0000-0000-0000-000000000017',
        '12', '11111111-0000-0000-0000-000000000002',
        'LinkedIn: 12 employees as of February 2025.',
        'analyst_alex', 0.88, TRUE),
    ('44444444-0000-0000-0000-000000000002', '33333333-0000-0000-0000-000000000018',
        '2023', '11111111-0000-0000-0000-000000000001',
        'Crunchbase: Founded 2023, Cairo, Egypt.',
        'analyst_alex', 0.99, TRUE);

-- NOTE: Founders Strength metrics are intentionally NOT inserted for HealthStack.
-- This causes overall_score = NULL and triggers the gap agent demo.


-- ----------------------------------------------------------
-- COMPANY C: GreenGrid — market signal is stale/low (market agent demo)
-- All metrics filled, but "Is in Growing Region" = false (Saudi CleanTech VC slow)
-- Running market_agent will refresh this and update the score
-- ----------------------------------------------------------

INSERT INTO company_metric_value (company_id, metric_id, value, source_id, raw_evidence, captured_by, confidence, is_latest) VALUES

-- Founders Strength
    ('44444444-0000-0000-0000-000000000003', '33333333-0000-0000-0000-000000000001',
        'false', '11111111-0000-0000-0000-000000000002',
        'LinkedIn + Crunchbase check: No prior exits found for either founder. First-time founders.',
        'analyst_alex', 0.88, TRUE),
    ('44444444-0000-0000-0000-000000000003', '33333333-0000-0000-0000-000000000002',
        '3', '11111111-0000-0000-0000-000000000002',
        'LinkedIn: CEO Khalid Al-Saud — 6 years at ACWA Power, energy sector background. Strong domain expertise.',
        'analyst_alex', 0.85, TRUE),
    ('44444444-0000-0000-0000-000000000003', '33333333-0000-0000-0000-000000000003',
        'true', '11111111-0000-0000-0000-000000000002',
        'LinkedIn: CTO Nora Ibrahim — computer science PhD from KAUST, 4 years at Siemens Energy.',
        'analyst_alex', 0.87, TRUE),

-- Technology Moat
    ('44444444-0000-0000-0000-000000000003', '33333333-0000-0000-0000-000000000004',
        'true', '11111111-0000-0000-0000-000000000007',
        'USPTO search: Patent US11847201B2 — "Modular battery pack with dynamic thermal regulation." Filed 2023.',
        'analyst_alex', 0.95, TRUE),
    ('44444444-0000-0000-0000-000000000003', '33333333-0000-0000-0000-000000000005',
        '3', '11111111-0000-0000-0000-000000000004',
        'Pitchdeck: Proprietary battery management firmware. No unique dataset moat yet.',
        'analyst_alex', 0.78, TRUE),
    ('44444444-0000-0000-0000-000000000003', '33333333-0000-0000-0000-000000000006',
        '3', '11111111-0000-0000-0000-000000000004',
        'Pitchdeck: Hardware switching costs are moderate — utility contracts typically 3-year lock-in.',
        'analyst_alex', 0.80, TRUE),

-- Market Growth — "Is in Growing Region" = FALSE (stale, will be updated by market agent)
    ('44444444-0000-0000-0000-000000000003', '33333333-0000-0000-0000-000000000007',
        'true', '11111111-0000-0000-0000-000000000006',
        'Statista: Global grid-scale battery storage CAGR 32% through 2030. Saudi Vision 2030 driving demand.',
        'analyst_alex', 0.90, TRUE),
    ('44444444-0000-0000-0000-000000000003', '33333333-0000-0000-0000-000000000008',
        'false', '11111111-0000-0000-0000-000000000005',
        'Tavily search (STALE — Oct 2024): Saudi CleanTech VC investment declined 18% in H1 2024 amid oil price uncertainty.',
        'market_agent', 0.60, TRUE),  -- Low confidence, stale — market_agent will refresh this
    ('44444444-0000-0000-0000-000000000003', '33333333-0000-0000-0000-000000000009',
        '4', '11111111-0000-0000-0000-000000000006',
        'Statista: MENA grid-scale storage TAM $8B by 2030, driven by Saudi and UAE Vision programs.',
        'analyst_alex', 0.85, TRUE),

-- Competitive Landscape
    ('44444444-0000-0000-0000-000000000003', '33333333-0000-0000-0000-000000000010',
        'false', '11111111-0000-0000-0000-000000000005',
        'Tavily: Limited regional competitors. Main players are Western (Tesla Megapack, Fluence) — no MENA-native player.',
        'analyst_alex', 0.82, TRUE),
    ('44444444-0000-0000-0000-000000000003', '33333333-0000-0000-0000-000000000011',
        '4', '11111111-0000-0000-0000-000000000004',
        'Pitchdeck: Local manufacturing + Arabic-language support + Vision 2030 government certification = strong differentiation vs Western players.',
        'analyst_alex', 0.85, TRUE),

-- Unit Economics
    ('44444444-0000-0000-0000-000000000003', '33333333-0000-0000-0000-000000000012',
        '3', '11111111-0000-0000-0000-000000000003',
        'Founder interview: LTV:CAC of 3.1x. Hardware business — longer payback than SaaS.',
        'analyst_alex', 0.80, TRUE),

-- Financials
    ('44444444-0000-0000-0000-000000000003', '33333333-0000-0000-0000-000000000015',
        '2', '11111111-0000-0000-0000-000000000001',
        'Crunchbase: Pre-Series A — $3.2M seed raised from Saudi Aramco Ventures and Wa'ed, closed Aug 2024.',
        'analyst_alex', 0.95, TRUE),

-- Company Demographics
    ('44444444-0000-0000-0000-000000000003', '33333333-0000-0000-0000-000000000017',
        '18', '11111111-0000-0000-0000-000000000002',
        'LinkedIn: 18 employees as of February 2025.',
        'analyst_alex', 0.88, TRUE),
    ('44444444-0000-0000-0000-000000000003', '33333333-0000-0000-0000-000000000018',
        '2022', '11111111-0000-0000-0000-000000000001',
        'Crunchbase: Founded 2022, Riyadh, Saudi Arabia.',
        'analyst_alex', 0.99, TRUE);


-- ============================================================
-- PIPELINE EVENTS  (stage history for NovaPay and GreenGrid)
-- ============================================================
INSERT INTO pipeline_event (company_id, from_stage, to_stage, changed_by, triggered_by, score_snapshot) VALUES
    ('44444444-0000-0000-0000-000000000001', 'deal_sourcing', 'first_contact',  'analyst_alex', 'manual',          3.8),
    ('44444444-0000-0000-0000-000000000001', 'first_contact',  'due_diligence', 'analyst_alex', 'score_threshold', 4.1),
    ('44444444-0000-0000-0000-000000000003', 'deal_sourcing', 'first_contact',  'analyst_alex', 'manual',          3.2);


-- ============================================================
-- PHASE 1 — BUILT-IN EXTRACTORS
-- Every metric_observation must reference an extractor. These three
-- cover the universe of how values currently enter the system:
--   seed_data_v1     — values baked into seed.sql (our demo dataset)
--   analyst_override — a human manually locked a value via the UI
--   alpha_scout_v1   — values produced by the discovery pipeline
-- Future extractors (pitchdeck_gemini_v1, meeting_note_md_v1, …)
-- will be registered as they are built. UUIDs are fixed so the
-- Phase 1 backfill script can reference them by ID.
-- ============================================================
INSERT INTO extractor (id, name, version, supported_source_types, description) VALUES
    ('55555555-0000-0000-0000-000000000001',
        'seed_data_v1', '1.0', '{seed}',
        'Values hand-authored in seed.sql for the demo dataset. Synthetic source document per company carries the seed text as evidence.'),
    ('55555555-0000-0000-0000-000000000002',
        'analyst_override', '1.0', '{analyst_note}',
        'Analyst manually set a value via the UI. Always wins the resolver (highest source priority).'),
    ('55555555-0000-0000-0000-000000000003',
        'alpha_scout_v1', '1.0', '{web_page}',
        'Alpha Scout discovery pipeline. Produces observations for Employee Count, Funding Stage, Founding Year based on Tavily + Gemini enrichment, grounded against trusted MENA / global sources.'),
    ('55555555-0000-0000-0000-000000000004',
        'pitchdeck_v1', '1.0', '{pitchdeck}',
        'Pitch-deck extractor (Phase 2a). Runs a deterministic rule engine (regex + enum_map) against per-slide chunks, with an LLM fallback for missing metrics. Provider-agnostic: defaults to Gemini Flash but any LLMProvider works.')
ON CONFLICT (name, version) DO NOTHING;
