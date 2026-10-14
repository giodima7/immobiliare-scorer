// Lume dashboard — UI text (English, Italian) and the t() / language helpers. Plain script (no bundler), loaded by index.html;
// shares globals with the other assets/*.js files.
const STRINGS = {
  en: {
    // Tabs
    'tab.rentals':'Rentals','tab.map':'Map','tab.stats':'Stats',
    'tab.settings':'Settings',
    'scan.areas_skipped': 'areas skipped',
    'scan.skipped_help':  'These areas need slug mapping in area_slug_mapping.json. The scanner falls back to a single city-wide search when any area returns 0 results — those listings are still captured but not attributed to the specific area.',
    'settings.scan_areas':       'Scan Areas',
    'settings.scan_areas_sub':   'Choose which areas of each city Lume scans. Each area = one search request to Immobiliare and Idealista.',
    'settings.city':             'City',
    'settings.loading_areas':    'Loading areas…',
    'settings.mapping_missing':  'Mapping not yet configured for this city.',
    'settings.select_all':       '✓ Select all',
    'settings.clear_all':        '✗ Clear all',
    'settings.areas_active':     'active',
    'settings.zones':            'zones',
    'settings.save_areas':       'Save areas',
    'settings.areas_saved':      'Scan areas saved',
    'settings.areas_save_error': 'Could not save scan areas',
    // Header
    'header.listings_count':'{n} listings in {city} · {date}',
    'header.favourites':'Favourites','header.sign_in':'Sign in',
    'header.sign_out':'Sign out','header.live':'Live','header.snapshot':'Snapshot',
    'header.run_lume':'▶ Run Lume now','header.zones':'Zones',
    // City selector (multi-city expansion)
    'city.milano':'Milan','city.roma':'Rome','city.napoli':'Naples','city.la_maddalena':'La Maddalena',
    'city.switch':'Switch city','city.no_metro':'no metro',
    'city.alert_scope':'Alert for {city}',
    // Filters
    'filter.area':'Area','filter.fascia':'Fascia',
    'filter.max_rent':'Max rent €/mo',
    'filter.min_sqm':'Min m²','filter.min_rooms':'Min rooms',
    'filter.max_metro':'Max metro','filter.min_floor':'Min floor',
    'filter.elevator':'Elevator','filter.min_energy':'Min energy',
    'filter.min_score':'Min score','filter.source':'Source',
    'filter.listed':'Listed','filter.gems':'Gems','filter.sort':'Sort',
    'filter.all':'All','filter.any':'Any',
    'filter.show_expired':'Show expired','filter.start_fresh':'Start fresh',
    'filter.clear_filters':'Clear filters',
    'filter.hidden':'✦ Hidden','filter.great_value':'Great Value',
    // Save-alert button (Supabase digest) — visible on rentals tab when signed in.
    'filter.save_alert':'🔔 Save alert','filter.saving':'⏳ Saving…','filter.alert_saved':'✓ Alert saved',
    // Sort buttons
    'sort.score':'Score','sort.rent_asc':'Rent ↑',
    // Summary bar
    'summary.showing':'Showing {n} listings',
    'summary.avg_score':'Avg score {score} / 100',
    'summary.avg_rent':'Avg rent €{rent}/mo','summary.avg_price':'Avg price €{price}',
    'summary.hidden_gems':'Hidden Gems {n}','summary.geo':'Geo:',
    'summary.rescore':'Rescore','summary.enrich':'Enrich',
    // Gem badges
    'badge.hidden_gem':'✦ Hidden Gem','badge.great_value':'💰 Great Value',
    'badge.new':'New','badge.expired':'Expired',
    // Card footer
    'card.view':'↗ View','card.fav':'♡ Fav','card.share':'Share',
    'card.archive_prompt': 'This listing appears to be unavailable.',
    'card.archive':        'Archive',
    'card.archived_ok':    'Archived — hidden from results',
    'card.archive_failed': 'Could not archive listing',
    'card.how_lume_scored':'How Lume scored this',
    'card.full_analysis':'📊 Full investment analysis',
    // Confidence labels
    'conf.high':'Lume is confident','conf.medium':'Lume is fairly sure',
    'conf.low':'Lume is uncertain','conf.thin':'Lume isn\'t sure — thin data',
    // Callout headlines
    'callout.hidden_gem':'Hidden Gem — {pct}% below similar flats nearby ({psqm} · {conf}).',
    'callout.great_value':'Great Value — {pct}% below similar flats nearby ({psqm} · {conf}).',
    'callout.below_market':'{pct}% below similar flats nearby ({psqm} · {conf}).',
    'callout.at_market':'In line with similar flats nearby ({psqm} · {conf}).',
    'callout.above_market':'{pct}% above similar flats nearby ({psqm} · {conf}).',
    // Comps suggest
    'comps.suggest':'⚠ Comps suggest:','comps.diff_rent':'(+€{diff} vs {n} similar flats nearby)',
    'comps.diff_psqm':'(+€{diff}/m² vs {n} nearby sales)',
    // Score reason components (badges in card)
    'reason.component.price':'PRICE','reason.component.property':'PROPERTY',
    'reason.component.location':'LOCATION','reason.component.penalty':'PENALTY',
    'reason.component.yield':'YIELD','reason.component.demand':'DEMAND',
    'reason.component.costs':'COSTS','reason.component.returns':'RETURNS',
    'reason.component.upside':'UPSIDE','reason.component.liquidity':'LIQUIDITY',
    // Score reasons — Price
    'reason.price.far_below':'{pct}% below similar flats nearby',
    'reason.price.below':'{pct}% below comparable listings — modest discount',
    'reason.price.at_market':'Priced in line with similar flats nearby',
    'reason.price.above':'{pct}% above comparable listings — slight premium',
    'reason.price.far_above':'{pct}% above similar flats nearby',
    'reason.price.gate':'€{psqm}/m² is poor absolute value — score capped at {ceiling}',
    'reason.price.high_condo':'€{fee}/mo condo fees push the effective cost up',
    'reason.price.corporate':'Corporate / short-term rental — priced for furnished flexible lets',
    'reason.price.strong_entry':'{pct}% below similar flats — strong entry point',
    // Score reasons — Location
    'reason.location.very_close':'{min} min walk to {name}{line}',
    'reason.location.close':'{min} min to {name}{line}',
    'reason.location.far':'Far from metro — {min} min walk',
    'reason.location.limited':'Limited transport — {min} min to nearest metro',
    'reason.location.bargain':'Bargain in a highly desirable area — Lume rarely sees this combination',
    'reason.location.weak_zone':'Lower-demand zone — limited resale and rental appeal',
    'reason.location.below_avg':'Below-average location — {items}',
    'reason.location.quiet':'Quieter / less-connected pocket of the city',
    // Score reasons — Property
    'reason.property.subterranean':'Below ground floor — Lume would walk away',
    'reason.property.ground':'Ground floor — light and noise are usually issues',
    'reason.property.rialzato':'Piano rialzato — slightly elevated but street noise and limited light remain',
    'reason.property.no_lift':'Floor {n} without a lift — significant drawback',
    'reason.property.renovation':'Needs renovation — factor in upfront work',
    'reason.property.strengths':'{items}',
    'reason.property.below_avg':'Below average for the area — {items}',
    'reason.property.limited':'Limited standout features for the asking price',
    'reason.property.studio_partition':'{rooms} rooms in {sqm}m² — likely a studio with partition',
    'reason.property.cramped_bilo':'Tight for a bilocale at {sqm}m²',
    'reason.property.cramped_layout':'Cramped layout for the room count',
    'reason.property.small_studio':'Small studio at {sqm}m²',
    'reason.property.old_unrenovated':'Built {year} and not renovated — likely poor insulation',
    'reason.property.recently_renovated':'Recently renovated, floor {n} with lift',
    'reason.property.recently_renovated_baths':'Recently renovated, floor {n} with lift, {b} bathrooms',
    'reason.property.str.renovated':'recently renovated',
    'reason.property.str.floor_lift':'floor {n} with lift',
    'reason.property.str.bathrooms':'{n} bathrooms',
    'reason.property.str.balcony':'balcony',
    'reason.property.str.post2010':'post-2010 build',
    'reason.property.str.no_outdoor':'no outdoor space',
    'reason.property.str.basic_cond':'basic condition',
    'reason.property.str.no_lift_low':'floor {n} without confirmed lift',
    'reason.property.str.old_unreno':'{year}s build, not renovated',
    // Score reasons — Penalty
    'reason.penalty.stale':'On market {days} days — Lume notices it\'s not moving',
    'reason.penalty.fresh':'Fresh listing — just appeared',
    // Score reasons — Investor / Yield
    'reason.returns.neg_cf_normal':'Negative cash flow normal for Milan — {pct}% annualised over 5yr including appreciation',
    'reason.returns.neg_cf_weak':'Even with appreciation, projected 5yr return is weak ({pct}% annualised)',
    'reason.investor.high_yield':'{pct}% gross yield — above Milan average',
    'reason.investor.low_yield':'{pct}% gross yield — below typical Milan investment threshold',
    'reason.investor.typical_yield':'{pct}% gross yield — typical for the area',
    'reason.investor.strong_demand':'Strong rental demand — desirable area, close to metro',
    'reason.investor.weak_demand':'Lower-demand zone — longer vacancies, weaker rent growth',
    'reason.investor.high_condo':'High condo fees (€{fee}/mo) — drag on net yield',
    'reason.investor.stale_negotiate':'On market {days} days — seller may negotiate',
    'reason.investor.low_conf':'Rent estimate based on limited comps — verify before offer',
    'reason.investor.strong_yield':'Strong yield — ~{pct}%/yr gross',
    'reason.investor.weak_yield_pct':'Weak yield — ~{pct}%/yr gross',
    // Empty / loading
    'empty.no_results':'Nothing matches yet.',
    'empty.loosen':'Loosen up a filter.',
    'loading.listings':'Lume is reading the listings…',
    'error.supabase':'Couldn\'t reach Supabase — try refreshing.',
    'error.supabase_unconfigured':'Supabase not configured — listings unavailable.',
    'gem.all':'All','gem.hidden':'✦ Hidden Gems','gem.good':'Great Value',
    'stats.showing':'Showing','stats.rentals':'rentals','stats.avg_score':'Avg score','stats.avg_rent':'Avg rent',
    'stats.new_3d':'new in the last 3 days','stats.zone_fallback':'Zone by name',
    'stats.zone_fallback_tip':'Listings placed in an OMI zone by neighbourhood name, not by coordinates',
    'scan.stopped':'Scanner stopped',
    'scan.scanning':'Scanning rentals…',
    'scan.daily_on':'Scanner on · one scan a day',
    'scan.idealista_scanning':'Idealista scanning…',
    'scan.idealista_on':'Idealista on · daily',
    'scan.idealista_stopped':'Idealista stopped',
    'error.partial':'some listings didn\'t load — refresh to retry',
    'sort.discount':'Discount',
    'sort.newest':'Newest',
    'card.data_show':'Show data points',
    'card.data_hide':'Hide data points',
    'card.suspect':'Unusually cheap for the area — check the listing is real and still available before trusting the discount.',
    'card.seen_today':'New today',
    'card.seen_days':'First seen {n} d ago',
    'card.comps_show':'Compare with the {n} nearby flats',
    'card.comps_loading':'Loading the comparable flats…',
    'card.comps_none':'Comparable flats not available.',
    'card.comps_archived':'no longer online',
    'card.comps_hidden':'+ {n} older listings, no longer online (visible on the local dashboard)',
    'card.twin':'Some details come from the same flat\'s listing on {site}.',
    'card.cap':'Score capped: {reason}',
    'card.z':'Discount strength',
    'card.z_val':'{z}× the usual spread nearby (labels need {gv}× or more)',
    'alerts.local_only':'Email alerts are managed from the local dashboard (localhost:8000).',
    // Settings tab — Daily alert section (Supabase digest_filters)
    'settings.alert':'Daily alert',
    'settings.alert_active':'Active','settings.alert_paused':'Paused',
    'settings.alert_sent_to':'Sent to {email} each morning with new listings matching:',
    'settings.alert_none':'No alert set up yet. Go to Rentals, set your filters, and click 🔔 Save alert.',
    'settings.pause_alert':'⏸ Pause alert','settings.resume_alert':'▶ Resume alert',
    'settings.update_filters':'✏️ Update filters',
    // Toasts
    'toast.alert_saved':'Daily alert saved — you\'ll get an email with matching new listings each day',
    'toast.alert_paused':'Alert paused','toast.alert_resumed':'Alert resumed',
    'toast.sign_in_required':'Sign in to save alerts',
    // Save-alert modal — opened from the rentals filter bar Save alert pill.
    'alert_modal.title':'🔔 Daily email alert',
    'alert_modal.subtitle':'Review the filters below — Lume will email you each day with new listings (and price drops) that match.',
    'alert_modal.close':'Close',
    'alert_modal.section_filters':'Filters',
    'alert_modal.section_delivery':'Delivery',
    'alert_modal.send_time':'Send time (your local)',
    'alert_modal.send_time_hint':'Hourly resolution. Stored as UTC.',
    'alert_modal.min_matches':'Min matches to send',
    'alert_modal.min_matches_hint':'Skip email if fewer matches that day.',
    'alert_modal.cancel':'Cancel',
    'alert_modal.save':'💾 Save alert',
    'alert_modal.error':'Could not save alert:',
    'alert_modal.title_edit':'✏️ Edit alert',
    'alert_modal.save_changes':'💾 Save changes',
    'alert_modal.section_name':'Alert name',
    'alert_modal.name_placeholder':'e.g. Centro 2-bed under €1500',
    'alert_modal.name_hint':'Optional — helps you tell multiple alerts apart in Settings.',
    'alert_modal.delete_confirm':'Delete this alert? You won\'t receive its emails anymore.',
    // Settings — daily alerts list
    'settings.alerts':'Daily alerts',
    'settings.new_alert':'+ New alert',
    'settings.alert_unnamed':'Unnamed alert',
    'settings.alert_all':'All listings (no filters)',
    'settings.alert_sends_at':'Sends at',
    'settings.alert_min_n':'min {n}',
    'settings.delete_alert':'🗑 Delete',
    'toast.alert_deleted':'Alert deleted',
    'toast.alert_delete_failed':'Could not delete alert:',
    // Avatar dropdown menu — entries the user lands in via the avatar
    // pill in the header. The Settings tab is hidden on the deployed
    // dashboard, so the avatar menu is the canonical entry point for
    // signed-in account actions.
    'menu.my_alerts':'My alerts',
  },
  it: {
    // Tabs
    'tab.rentals':'Affitti','tab.map':'Mappa','tab.stats':'Statistiche',
    'tab.settings':'Impostazioni',
    'scan.areas_skipped': 'aree saltate',
    'scan.skipped_help':  'Queste aree richiedono mappatura slug in area_slug_mapping.json. Lo scanner ricade su una sola ricerca city-wide quando un\'area restituisce 0 risultati — quegli annunci sono comunque catturati ma non attribuiti alla zona specifica.',
    'settings.scan_areas':       'Aree di scansione',
    'settings.scan_areas_sub':   'Scegli quali aree di ogni città Lume scansiona. Ogni area = una richiesta di ricerca a Immobiliare e Idealista.',
    'settings.city':             'Città',
    'settings.loading_areas':    'Caricamento aree…',
    'settings.mapping_missing':  'Mappatura non ancora configurata per questa città.',
    'settings.select_all':       '✓ Seleziona tutte',
    'settings.clear_all':        '✗ Deseleziona tutte',
    'settings.areas_active':     'attive',
    'settings.zones':            'zone',
    'settings.save_areas':       'Salva aree',
    'settings.areas_saved':      'Aree salvate',
    'settings.areas_save_error': 'Impossibile salvare le aree',
    // Header
    'header.listings_count':'{n} annunci a {city} · {date}',
    'header.favourites':'Preferiti','header.sign_in':'Accedi',
    'header.sign_out':'Esci','header.live':'Live','header.snapshot':'Snapshot',
    'header.run_lume':'▶ Avvia Lume','header.zones':'Zone',
    // City selector (multi-city expansion)
    'city.milano':'Milano','city.roma':'Roma','city.napoli':'Napoli','city.la_maddalena':'La Maddalena',
    'city.switch':'Cambia città','city.no_metro':'senza metro',
    'city.alert_scope':'Avviso per {city}',
    // Filters
    'filter.area':'Area','filter.fascia':'Fascia',
    'filter.max_rent':'Affitto max €/me',
    'filter.min_sqm':'Min m²','filter.min_rooms':'Min locali',
    'filter.max_metro':'Max metro','filter.min_floor':'Piano min',
    'filter.elevator':'Ascensore','filter.min_energy':'Energia min',
    'filter.min_score':'Score min','filter.source':'Fonte',
    'filter.listed':'Inserito','filter.gems':'Opportunità','filter.sort':'Ordina',
    'filter.all':'Tutti','filter.any':'Qualsiasi',
    'filter.show_expired':'Mostra scaduti','filter.start_fresh':'Azzera filtri',
    'filter.clear_filters':'Azzera filtri',
    'filter.hidden':'✦ Gemme','filter.great_value':'Ottimo valore',
    // Save-alert button (Supabase digest) — visible on rentals tab when signed in.
    'filter.save_alert':'🔔 Salva avviso','filter.saving':'⏳ Salvataggio…','filter.alert_saved':'✓ Avviso salvato',
    // Sort buttons
    'sort.score':'Score','sort.rent_asc':'Affitto ↑',
    // Summary bar
    'summary.showing':'Mostra {n} annunci',
    'summary.avg_score':'Score medio {score} / 100',
    'summary.avg_rent':'Affitto medio €{rent}/me','summary.avg_price':'Prezzo medio €{price}',
    'summary.hidden_gems':'Gemme nascoste {n}','summary.geo':'Geo:',
    'summary.rescore':'Ricalcola','summary.enrich':'Arricchisci',
    // Gem badges
    'badge.hidden_gem':'✦ Gemma Nascosta','badge.great_value':'💰 Ottimo Valore',
    'badge.new':'Nuovo','badge.expired':'Scaduto',
    // Card footer
    'card.view':'↗ Vedi','card.fav':'♡ Preferito','card.share':'Condividi',
    'card.archive_prompt': 'Questo annuncio sembra non essere più disponibile.',
    'card.archive':        'Archivia',
    'card.archived_ok':    'Archiviato — nascosto dai risultati',
    'card.archive_failed': 'Impossibile archiviare l\'annuncio',
    'card.how_lume_scored':'Come ha valutato Lume',
    'card.full_analysis':'📊 Analisi investimento completa',
    // Confidence labels
    'conf.high':'Lume è sicuro','conf.medium':'Lume è abbastanza sicuro',
    'conf.low':'Lume non è certo','conf.thin':'Lume non è sicuro — dati scarsi',
    // Callout headlines
    'callout.hidden_gem':'Gemma Nascosta — {pct}% sotto annunci simili ({psqm} · {conf}).',
    'callout.great_value':'Ottimo Valore — {pct}% sotto annunci simili ({psqm} · {conf}).',
    'callout.below_market':'{pct}% sotto annunci simili ({psqm} · {conf}).',
    'callout.at_market':'In linea con annunci simili ({psqm} · {conf}).',
    'callout.above_market':'{pct}% sopra annunci simili ({psqm} · {conf}).',
    // Comps suggest
    'comps.suggest':'⚠ Comparabili suggeriscono:',
    'comps.diff_rent':'(+€{diff} vs {n} annunci simili nella zona)',
    'comps.diff_psqm':'(+€{diff}/m² vs {n} vendite vicine)',
    // Score reason components (badges in card)
    'reason.component.price':'PREZZO','reason.component.property':'IMMOBILE',
    'reason.component.location':'POSIZIONE','reason.component.penalty':'PENALITÀ',
    'reason.component.yield':'RENDIMENTO','reason.component.demand':'DOMANDA',
    'reason.component.costs':'COSTI','reason.component.returns':'RENDIMENTO',
    'reason.component.upside':'POTENZIALE','reason.component.liquidity':'LIQUIDITÀ',
    // Score reasons — Price
    'reason.price.far_below':'{pct}% sotto annunci simili nella zona',
    'reason.price.below':'{pct}% sotto i comparabili — sconto moderato',
    'reason.price.at_market':'In linea con gli annunci simili nella zona',
    'reason.price.above':'{pct}% sopra i comparabili — leggero sovrapprezzo',
    'reason.price.far_above':'{pct}% sopra annunci simili nella zona',
    'reason.price.gate':'€{psqm}/m² ha scarso valore assoluto — score limitato a {ceiling}',
    'reason.price.high_condo':'Spese condominiali €{fee}/me aumentano il costo effettivo',
    'reason.price.corporate':'Affitto corporate/breve termine — prezzo per contratti flessibili arredati',
    'reason.price.strong_entry':'{pct}% sotto annunci simili — ottimo punto di ingresso',
    // Score reasons — Location
    'reason.location.very_close':'{min} min a piedi da {name}{line}',
    'reason.location.close':'{min} min da {name}{line}',
    'reason.location.far':'Lontano dalla metro — {min} min a piedi',
    'reason.location.limited':'Trasporti limitati — {min} min dalla metro più vicina',
    'reason.location.bargain':'Affare in una zona molto desiderabile — Lume lo vede raramente',
    'reason.location.weak_zone':'Zona a bassa domanda — rivendita e locazione difficili',
    'reason.location.below_avg':'Posizione sotto la media — {items}',
    'reason.location.quiet':'Zona più tranquilla / meno collegata della città',
    // Score reasons — Property
    'reason.property.subterranean':'Piano interrato — Lume non lo consiglierebbe',
    'reason.property.ground':'Piano terra — problemi di luce e rumore tipici',
    'reason.property.rialzato':'Piano rialzato — rumore stradale e luce limitata rimangono un problema',
    'reason.property.no_lift':'Piano {n} senza ascensore — svantaggio significativo',
    'reason.property.renovation':'Da ristrutturare — considera i costi iniziali',
    'reason.property.strengths':'{items}',
    'reason.property.below_avg':'Sotto la media per la zona — {items}',
    'reason.property.limited':'Caratteristiche limitate per il prezzo richiesto',
    'reason.property.studio_partition':'{rooms} locali in {sqm}m² — probabilmente monolocale con divisori',
    'reason.property.cramped_bilo':'Spazio ristretto per un bilocale a {sqm}m²',
    'reason.property.cramped_layout':'Layout stretto per il numero di locali',
    'reason.property.small_studio':'Monolocale piccolo a {sqm}m²',
    'reason.property.old_unrenovated':'Costruito nel {year} e non ristrutturato — probabilmente scarsa coibentazione',
    'reason.property.recently_renovated':'Recentemente ristrutturato, piano {n} con ascensore',
    'reason.property.recently_renovated_baths':'Recentemente ristrutturato, piano {n} con ascensore, {b} bagni',
    'reason.property.str.renovated':'recentemente ristrutturato',
    'reason.property.str.floor_lift':'piano {n} con ascensore',
    'reason.property.str.bathrooms':'{n} bagni',
    'reason.property.str.balcony':'balcone',
    'reason.property.str.post2010':'costruzione post-2010',
    'reason.property.str.no_outdoor':'nessuno spazio esterno',
    'reason.property.str.basic_cond':'condizioni base',
    'reason.property.str.no_lift_low':'piano {n} senza ascensore confermato',
    'reason.property.str.old_unreno':'costruzione anni {year}, non ristrutturata',
    // Score reasons — Penalty
    'reason.penalty.stale':'In vendita da {days} giorni — Lume nota che non si muove',
    'reason.penalty.fresh':'Annuncio fresco — appena pubblicato',
    // Score reasons — Investor / Yield
    'reason.returns.neg_cf_normal':'Flusso di cassa negativo normale a Milano — {pct}% annualizzato in 5 anni inclusa rivalutazione',
    'reason.returns.neg_cf_weak':'Anche con la rivalutazione, il rendimento a 5 anni è debole ({pct}% annualizzato)',
    'reason.investor.high_yield':'{pct}% rendimento lordo — sopra la media milanese',
    'reason.investor.low_yield':'{pct}% rendimento lordo — sotto la soglia tipica per investimenti a Milano',
    'reason.investor.typical_yield':'{pct}% rendimento lordo — nella media della zona',
    'reason.investor.strong_demand':'Forte domanda locatizia — zona desiderabile, vicino alla metro',
    'reason.investor.weak_demand':'Zona a bassa domanda — vacancy più lunga, crescita affitti più lenta',
    'reason.investor.high_condo':'Spese condominiali elevate (€{fee}/me) — impatto negativo sul rendimento netto',
    'reason.investor.stale_negotiate':'In vendita da {days} giorni — il venditore potrebbe trattare',
    'reason.investor.low_conf':'Stima affitto basata su pochi comparabili — verifica prima dell\'offerta',
    'reason.investor.strong_yield':'Rendimento solido — ~{pct}%/anno lordo',
    'reason.investor.weak_yield_pct':'Rendimento debole — ~{pct}%/anno lordo',
    // Empty / loading
    'empty.no_results':'Nessun risultato corrisponde.',
    'empty.loosen':'Allenta qualche filtro.',
    'loading.listings':'Lume sta leggendo gli annunci…',
    'error.supabase':'Impossibile raggiungere Supabase — ricarica la pagina.',
    'error.supabase_unconfigured':'Supabase non configurato — annunci non disponibili.',
    'gem.all':'Tutti','gem.hidden':'✦ Gemme nascoste','gem.good':'Ottimo valore',
    'stats.showing':'Mostra','stats.rentals':'affitti','stats.avg_score':'Score medio','stats.avg_rent':'Affitto medio',
    'stats.new_3d':'nuovi negli ultimi 3 giorni','stats.zone_fallback':'Zona dal nome',
    'stats.zone_fallback_tip':'Annunci assegnati a una zona OMI dal nome del quartiere, non dalle coordinate',
    'scan.stopped':'Scanner fermo',
    'scan.scanning':'Scansione affitti…',
    'scan.daily_on':'Scanner attivo · una scansione al giorno',
    'scan.idealista_scanning':'Idealista in scansione…',
    'scan.idealista_on':'Idealista attivo · giornaliero',
    'scan.idealista_stopped':'Idealista fermo',
    'error.partial':'alcuni annunci non sono stati caricati — ricarica per riprovare',
    'sort.discount':'Sconto',
    'sort.newest':'Più recenti',
    'card.data_show':'Mostra i dati',
    'card.data_hide':'Nascondi i dati',
    'card.suspect':'Insolitamente economico per la zona — verifica che l\'annuncio sia reale e ancora disponibile prima di fidarti dello sconto.',
    'card.seen_today':'Nuovo oggi',
    'card.seen_days':'Visto la prima volta {n} g fa',
    'card.comps_show':'Confronta con i {n} appartamenti vicini',
    'card.comps_loading':'Carico gli appartamenti di confronto…',
    'card.comps_none':'Appartamenti di confronto non disponibili.',
    'card.comps_archived':'non più online',
    'card.comps_hidden':'+ {n} annunci più vecchi, non più online (visibili nella dashboard locale)',
    'card.twin':'Alcuni dettagli vengono dall\'annuncio dello stesso appartamento su {site}.',
    'card.cap':'Punteggio limitato: {reason}',
    'card.z':'Forza dello sconto',
    'card.z_val':'{z}× la variabilità tipica in zona (le etichette richiedono almeno {gv}×)',
    'alerts.local_only':'Gli avvisi email si gestiscono dalla dashboard locale (localhost:8000).',
    // Settings tab — Daily alert section (Supabase digest_filters)
    'settings.alert':'Avviso giornaliero',
    'settings.alert_active':'Attivo','settings.alert_paused':'In pausa',
    'settings.alert_sent_to':'Inviato a {email} ogni mattina con i nuovi annunci corrispondenti:',
    'settings.alert_none':'Nessun avviso configurato. Vai su Affitti, imposta i filtri e clicca 🔔 Salva avviso.',
    'settings.pause_alert':'⏸ Metti in pausa','settings.resume_alert':'▶ Riprendi avviso',
    'settings.update_filters':'✏️ Aggiorna filtri',
    // Toasts
    'toast.alert_saved':'Avviso salvato — riceverai una email ogni giorno con i nuovi annunci corrispondenti',
    'toast.alert_paused':'Avviso messo in pausa','toast.alert_resumed':'Avviso ripreso',
    'toast.sign_in_required':'Accedi per salvare gli avvisi',
    // Save-alert modal
    'alert_modal.title':'🔔 Avviso email giornaliero',
    'alert_modal.subtitle':'Controlla i filtri qui sotto — Lume ti invierà una email ogni giorno con i nuovi annunci (e cali di prezzo) corrispondenti.',
    'alert_modal.close':'Chiudi',
    'alert_modal.section_filters':'Filtri',
    'alert_modal.section_delivery':'Consegna',
    'alert_modal.send_time':'Ora di invio (ora locale)',
    'alert_modal.send_time_hint':'Risoluzione oraria. Salvato come UTC.',
    'alert_modal.min_matches':'Min corrispondenze per inviare',
    'alert_modal.min_matches_hint':'Salta l\'email se ci sono meno corrispondenze.',
    'alert_modal.cancel':'Annulla',
    'alert_modal.save':'💾 Salva avviso',
    'alert_modal.error':'Impossibile salvare l\'avviso:',
    'alert_modal.title_edit':'✏️ Modifica avviso',
    'alert_modal.save_changes':'💾 Salva modifiche',
    'alert_modal.section_name':'Nome avviso',
    'alert_modal.name_placeholder':'es. Centro bilocale sotto €1500',
    'alert_modal.name_hint':'Opzionale — utile per distinguere più avvisi nelle Impostazioni.',
    'alert_modal.delete_confirm':'Eliminare questo avviso? Non riceverai più le sue email.',
    'settings.alerts':'Avvisi giornalieri',
    'settings.new_alert':'+ Nuovo avviso',
    'settings.alert_unnamed':'Avviso senza nome',
    'settings.alert_all':'Tutti gli annunci (nessun filtro)',
    'settings.alert_sends_at':'Invia alle',
    'settings.alert_min_n':'min {n}',
    'settings.delete_alert':'🗑 Elimina',
    'toast.alert_deleted':'Avviso eliminato',
    'toast.alert_delete_failed':'Impossibile eliminare l\'avviso:',
    'menu.my_alerts':'I miei avvisi',
  },
};

// Lume dashboard — the app. Plain script (no bundler), loaded by index.html;
// shares globals with the other assets/*.js files.
// ─── Internationalisation ─────────────────────────────────────────────────────
// Two-language UI (en / it). Static labels carry data-i18n="key" attributes
// so rerenderUI() can swap them without touching surrounding markup. Dynamic
// strings call t(key, vars) inline. Score reasons emitted by the Python
// scorer carry an i18n_key + i18n_vars structure (see explain.py); the
// dashboard translates them on render — same mechanism as inline strings.
//
// Keys use dot notation: 'category.subcategory.key'.
// Placeholders use {name}: t('reason.price.far_below', {pct: 12.3}).
//
// Add new strings here as the UI grows. Both en + it are required for any
// new key — t() falls back to en if it is missing, then to the raw key.
// STRINGS (the en/it UI text) lives in assets/i18n.js, loaded just before this file.

let currentLang = (function () {
  try {
    const saved = localStorage.getItem('lume_lang');
    if (saved && STRINGS[saved]) return saved;
  } catch (_) {}
  return (navigator.language || 'en').toLowerCase().startsWith('it') ? 'it' : 'en';
})();

// YYYY-MM-DD in local time (the scanner's dates are local calendar dates).
function _localDateStr(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function t(key, vars) {
  const dict = STRINGS[currentLang] || STRINGS.en;
  let s = (key in dict) ? dict[key] : (STRINGS.en[key] !== undefined ? STRINGS.en[key] : key);
  if (vars) {
    for (const k of Object.keys(vars)) {
      s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), vars[k] == null ? '' : String(vars[k]));
    }
  }
  return s;
}

// Translate a score-reason object emitted by the Python explainer.
// Defined further down in the file as reasonTextLocalised() — this stub
// is here just so any early caller (none today, but room to grow) gets a
// graceful undefined-safe behaviour before the main script block parses.
function reasonText(r) {
  if (typeof reasonTextLocalised === 'function') return reasonTextLocalised(r);
  if (!r) return '';
  if (r.i18n_key) return t(r.i18n_key, r.i18n_vars || {});
  return r.text || '';
}

function setLang(lang) {
  if (!STRINGS[lang] || lang === currentLang) {
    if (STRINGS[lang]) updateLangToggle();
    return;
  }
  currentLang = lang;
  document.documentElement.lang = lang;
  try { localStorage.setItem('lume_lang', lang); } catch (_) {}
  updateLangToggle();
  rerenderUI();
}

function updateLangToggle() {
  document.querySelectorAll('.lang-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.lang === currentLang);
  });
  document.documentElement.setAttribute('lang', currentLang);
}

function rerenderUI() {
  // Static labels — anything tagged data-i18n / data-i18n-placeholder /
  // data-i18n-title gets the matching string from the active language.
  document.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    el.title = t(el.dataset.i18nTitle);
  });
  // Dynamic content — re-run the renderers for whatever's on screen.
  if (typeof renderRentals === 'function' && Array.isArray(ALL_RENT) && ALL_RENT.length) renderRentals();
  // Meta line carries a locale-aware city name (Milan/Milano).
  if (typeof updateCityToggleUI === 'function') updateCityToggleUI();
}
