"""Invented histories and literal evidence probes, independent of model prompts."""


def exchange(user, assistant):
    return [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]


def fixture():
    sessions = [
        ("2024-03-01", "onboarding", [
            *exchange("My name is Lin Vale. I have accepted a full-time ceramics designer job at Oriole Atelier, starting March 4. Help me organize my onboarding checklist.",
                      "Your checklist for Oriole Atelier includes payroll, studio access and the kiln safety orientation before your March 4 start."),
            *exchange("I completed payroll and access today. I chose this studio because I can design ceramics there, rather than stay at my former retail job. My cousin Lin Moss also starts a new job: Pineworks in Osaka. Please keep our employment records separate.",
                      "Lin Vale is the ceramics designer at Oriole Atelier. Lin Moss is the cousin joining Pineworks in Osaka. These are separate people and employers."),
            *exchange("The kiln safety orientation is booked for March 4 and my onboarding checklist is now complete. The sketchbook is navy, the apron is beige, and I ordered six graphite pencils. None of those stationery choices changes my employer.",
                      "The completed onboarding record retains Oriole Atelier as Lin Vale's employer, alongside the scheduled safety orientation and separate stationery details."),
        ]),
        ("2024-03-12", "studio-move", [
            *exchange("I am checking the Oriole Atelier premises notice for our internal facilities log. It says the operating studio moved from Boston to Porto effective March 11. The registered postal address remains Boston. Help me distinguish these locations.",
                      "Use Porto for Oriole Atelier's operating studio from March 11, and Boston only for its retained registered postal address."),
            *exchange("I updated the facilities log exactly that way. No staff change occurred with the move. For parcel routing, the old loading door was blue and the Porto loading door is gray; the clay supplier still uses reference OA-48.",
                      "The facilities record now separates the operating studio in Porto from the Boston registered address. Door colors and OA-48 are delivery details."),
            *exchange("The premises correction is complete: Oriole Atelier operates in Porto as of March 11. Boston remains the registered postal address, not the current work site. Keep the previous location as history.",
                      "The completed notice preserves the old Boston site and the current Porto work site without treating the registered address as the operating location."),
        ]),
        ("2024-03-18", "client-workshop", [
            *exchange("I need to change my client pottery workshop plan. Yesterday I blamed rain for switching it online, but that was a mistaken explanation. The actual reason was the train cancellation and the client's fixed delivery deadline. Help me update the record.",
                      "The correction should say the train cancellation prevented travel and the fixed client delivery deadline made an online workshop necessary. Rain was the earlier mistaken explanation."),
            *exchange("Yes. The train cancellation alone might have allowed postponement, but the fixed delivery deadline meant postponement would miss the client's launch. Together they made me choose an online workshop. The slides use a sand-colored background and the client requested twelve sample bowls.",
                      "Both constraints belong in the decision record: train cancellation blocked travel, and the fixed delivery deadline ruled out postponement. Slide colors and sample quantity are separate presentation details."),
            *exchange("I finished the online workshop and corrected the explanation in my log. Please preserve that I had first blamed rain and then retracted it, rather than silently replacing the historical record.",
                      "The log now preserves the mistaken rain explanation as retracted history and records train cancellation plus the fixed delivery deadline as the actual reasons for the online workshop."),
        ]),
        ("2024-03-20", "visit-booking", [
            *exchange("Help me record my rail booking for a museum visit. I initially wrote March 21, but I checked the ticket: departure is March 22 at 09:10, local time. March 21 is only the date I will pack my bag.",
                      "The museum rail trip departs March 22 at 09:10 local time. March 21 is the packing date, and the earlier departure entry needs correction."),
            *exchange("I corrected the trip calendar and kept the original mistaken March 21 departure entry crossed out. My cousin Lin Moss has a different museum visit on March 25 from Osaka. Mine is unrelated to that booking.",
                      "Lin Vale's corrected departure is March 22 at 09:10 local time. Lin Moss's March 25 visit from Osaka remains a separate booking."),
            *exchange("Booking verification is complete. I will pack on March 21 and depart on March 22 at 09:10 local time. The seat is 18A, the bag is olive green, and I chose a vegetarian sandwich for the journey.",
                      "The finalized calendar keeps packing and departure on separate days, with the corrected March 22 departure and its local 09:10 time."),
        ]),
        ("2024-05-03", "second-studio-move", [
            *exchange("There is a new Oriole Atelier premises notice for the facilities log. The operating studio moved again, from Porto to Valencia effective May 2. Boston is still only the registered postal address. Please update the chronology without deleting the March move.",
                      "Record Boston to Porto effective March 11, then Porto to Valencia effective May 2. The latest operating studio is Valencia; Boston remains the registered postal address."),
            *exchange("The facilities log has been updated. The employer and staff roles are unchanged by this second move. The new storage racks are orange, the packing tape is white, and clay supplier OA-48 stays the same.",
                      "The second premises update changes Oriole Atelier's operating site to Valencia without changing employment or staff roles. Storage colors and supplier reference are incidental."),
            *exchange("The May update is complete: Oriole Atelier currently operates in Valencia since May 2. Porto was the operating site from March 11 until that move; Boston is the older site and continuing registered address.",
                      "The finalized premises chronology distinguishes current Valencia, previous Porto and the Boston registered address. It does not treat an old or postal location as the current work site."),
        ]),
        ("2024-05-04", "unfinished-tail", [
            {"role": "user", "content": "One unfinished note: I use stainless steel shelf brackets to resist salty sea air at my coastal store room. I haven't asked for a plan or completed this upgrade yet."},
        ]),
    ]
    # Each unit is an AND of literal phrases, with alternative spellings per phrase.
    # Probes measure evidence presence, not entailment or generated-answer accuracy.
    affiliation = [["Lin Vale"], ["Oriole Atelier"]]
    latest = [["Oriole Atelier"], ["Valencia", "瓦伦西亚"], ["May 2", "2024-05-02", "5月2日"]]
    previous = [["Oriole Atelier"], ["Porto", "波尔图"], ["March 11", "2024-03-11", "3月11日"]]
    cousin = [["Lin Moss"], ["Pineworks"], ["Osaka", "大阪"]]
    cases = [
        {"id": "current-work-city", "tags": ["cross-session", "multi-hop", "update", "same-given-name"],
         "query": "As of May 4, which city does Lin Vale work in, and what evidence connects this person to the latest work site?",
         "required": {"affiliation": affiliation, "latest-operating-site": latest}, "distractors": {"cousin-employment": cousin}},
        {"id": "location-chronology", "tags": ["cross-session", "dates", "history"],
         "query": "What were the successive operating cities of Oriole Atelier in March and May, and which city stayed its registered postal address?",
         "required": {"march-site": previous, "may-site": latest,
                      "registered-address": [["Oriole Atelier"], ["Boston", "波士顿"], ["registered", "注册"]]}, "distractors": {"cousin-employment": cousin}},
        {"id": "open-decision-reason", "tags": ["open-relation", "correction", "negation"],
         "query": "What combination of constraints made me switch my client pottery workshop online, and which earlier explanation did I retract?",
         "required": {"travel-block": [["train cancellation", "cancelled train", "canceled train", "火车取消"], ["online", "线上"]],
                      "deadline": [["deadline", "交付期限", "截止日期"], ["postponement", "postpone", "launch", "延期", "上线"]],
                      "retracted-rain": [["rain", "下雨"], ["mistaken", "retracted", "retract", "incorrect", "误判", "撤回", "错误"]]},
         "distractors": {"cousin-employment": cousin}},
        {"id": "corrected-booking", "tags": ["date", "correction", "noise"],
         "query": "When is my corrected museum train departure in local time, and what does the earlier March 21 date refer to?",
         "required": {"departure": [["March 22", "2024-03-22", "3月22日"], ["09:10", "9:10"], ["local", "当地", "本地"]],
                      "packing": [["March 21", "2024-03-21", "3月21日"], ["pack", "packing", "打包", "收拾"]]},
         "distractors": {"cousin-trip": [["Lin Moss"], ["March 25", "2024-03-25", "3月25日"]]}},
        {"id": "identity-separation", "tags": ["same-given-name", "identity"],
         "query": "Which employer and city belong to my cousin Lin Moss, rather than Lin Vale?",
         "required": {"cousin-employment": cousin}, "distractors": {"other-employment": affiliation}},
        {"id": "unfinished-original", "tags": ["raw-original", "paraphrase"],
         "query": "Which metal did I choose for the coastal store room shelf supports against corrosion from ocean spray?",
         "required": {"bracket-metal": [["stainless steel"], ["bracket", "brackets", "support", "supports"], ["salty", "sea", "coastal"]]},
         "distractors": {"cousin-employment": cousin}},
    ]
    return sessions, cases
