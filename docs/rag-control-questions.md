# Контрольные вопросы для сравнения RAG

В демо сначала включается `/rules mode off` и задаются все десять вопросов по порядку. Затем история один раз очищается командой `/clear`, включается `/rules mode on` и те же десять вопросов задаются в том же порядке. Модель и настройки не меняются. Вопросы сформулированы по-английски, как индексируемые PDF: локальный поиск использует лексические эмбеддинги. Ожидаемые факты оцениваются по ответам, источник — отдельно по названию PDF под ответом. Сравнение выполняется в демо, а не агентом.

| № | Вопрос | Ожидание: что должно быть в ответе | Ожидаемый источник |
| --- | --- | --- | --- |
| 1 | In CATAN, what resources are required to build a road? | Одна карта кирпича и одна карта дерева. | `catan-rules.pdf` |
| 2 | In CATAN, how many resource cards does a city receive for a production roll? | Две карты ресурса этого гекса. | `catan-rules.pdf` |
| 3 | In CATAN, what are the requirements and victory points for Longest Road? | Первым построить непрерывную дорогу из минимум пяти сегментов; карта даёт два победных очка. | `catan-rules.pdf` |
| 4 | In CATAN, what happens to resource cards when rolling a 7 if a player has more than 7? | Ресурсы никто не получает; игрок с более чем семью картами сбрасывает половину, округляя вниз. | `catan-rules.pdf` |
| 5 | In CATAN, how many knight cards are needed for Largest Army and how many victory points? | Первым сыграть три карты рыцаря; карта даёт два победных очка. | `catan-rules.pdf` |
| 6 | At setup in Ticket to Ride, how many Destination Ticket cards are dealt to each player and how many must be kept? | Получает три билета, оставляет минимум два. | `ticket-to-ride-rules.pdf` |
| 7 | In Ticket to Ride, which three actions can a player choose on their game turn: drawing cards, claiming a route, or what else? | Взять карты вагонов, занять маршрут или взять билеты назначения; выбирается одно действие. | `ticket-to-ride-rules.pdf` |
| 8 | In Ticket to Ride, what happens when drawing a face-up Locomotive card? | Только одну карту за ход: открытый локомотив заменяет обычный выбор двух карт. | `ticket-to-ride-rules.pdf` |
| 9 | In Ticket to Ride, what happens to an incomplete Destination Ticket at final scoring? | Вычитает номинал именно этого билета, указанный на карте. | `ticket-to-ride-rules.pdf` |
| 10 | In Ticket to Ride, when does the game end after one player has 0, 1 or 2 trains left? | Каждый игрок, включая этого, делает ещё один ход, затем партия заканчивается. | `ticket-to-ride-rules.pdf` |

Сравнение в видео: для каждой строки отметить правильность фактов в ответе без RAG и с RAG, затем проверить наличие ожидаемого PDF в источниках режима RAG. Наличие источника без верных фактов не считается верным ответом.
