# Keycloak 設定：一步一步

[English](keycloak-setup.md)

這份文件帶你把 gateway 接上 Keycloak，從 Keycloak 管理介面的第一個按鈕到切換當天。做完之後：

- 網頁（dashboard、admin）用 Keycloak 登入，取代 AuthCenter；
- gateway 每小時向 Keycloak 查詢離職與調職的人。

API 使用者完全不受影響：他們用的是 API key，帳號、key、額度、用量都不變。

> 管理介面的選單名稱以 Keycloak 19 之後的新版介面為準，各版本的字樣可能略有不同。這份文件沒有在你們的 Keycloak 上實際操作過，所以每做完一段就跑一次 preflight（第 7 步），由它告訴你對不對。

## 先看整體

要在 Keycloak 建**兩個 client**，用途不同，不要共用：

| Client | 用途 | 誰拿它的 secret | 權限 |
|---|---|---|---|
| `llm_gateway` | 人的登入 | oauth2-proxy（`deploy/.env`） | 兩個 client role：`read`、`admin` |
| `llm-gateway-offboarding` | 查詢誰被停用、誰換部門，以及 preflight | gateway（`.env`） | 唯讀：`view-users` |

**建議的順序**：Keycloak 端一次設定好（第 1 ~ 6 步），preflight 全部通過後，再分兩次上線。

1. 先開離職檢查（第 8 步）。它不動登入，沒有人會察覺，而且會用真實資料驗證連線與帳號名稱。
2. 再切換登入（第 9 步）。這一步所有人要重新登入一次，隨時可以退回（第 10 步）。

如果 preflight 沒有任何 FAIL，兩步同一天做也可以；分開做只是讓切換當天少一個變數。

## 1. 事前確認

動手前先確定這四件事，後面的步驟都用得到：

| 要確認的 | 為什麼 |
|---|---|
| Realm 的 URL：`https://<keycloak>/realms/<realm>` | 所有設定都以它為準，必須跟 token 裡的 `iss` 一字不差 |
| Keycloak 上的 **username** 跟 gateway 的帳號名稱相同 | 帳號靠 username 對應。不同的話，那個人登入會進到一個新的空帳號 |
| 離職的人在 Keycloak 是被**停用**（Enabled 關閉），不是被刪除 | 離職檢查只認「已停用」 |
| Keycloak 的憑證是誰簽的（公開 CA，還是公司內部 CA） | 內部 CA 要另外給 gateway 和 oauth2-proxy 一份 CA 憑證 |

Keycloak 會把 username 轉成小寫。先在 gateway 的資料庫查有沒有大寫的帳號：

```sql
SELECT username FROM users WHERE username <> lower(username) AND NOT is_service_account;
```

有的話切換前要先改成小寫（preflight 也會列出來）。

## 2. 建立登入用的 client

*Clients* → *Create client*

| 頁面 | 欄位 | 值 |
|---|---|---|
| General settings | Client type | `OpenID Connect` |
| | Client ID | `llm_gateway` |
| Capability config | Client authentication | **On** |
| | Authorization | Off |
| | Standard flow | **勾選** |
| | Direct access grants | **取消勾選** |
| | 其他 flow | 都不勾 |
| Login settings | Valid redirect URIs | `https://<gateway 的網域>/oauth2/callback` |
| | Web origins | 留空 |

存檔後到 *Credentials* 分頁，複製 **Client secret**。這是切換當天要填進 `deploy/.env` 的 `OAUTH2_CLIENT_SECRET`。

- Redirect URI 寫完整的那一個網址，不要用 `*`：萬用字元會讓別的網站也能接到登入結果。
- Direct access grants 關閉：它讓程式可以拿使用者的密碼換 token，gateway 用不到。

## 3. 建立兩個 client role

*Clients* → `llm_gateway` → *Roles* 分頁 → *Create role*，建兩個：

| Role name | 意義 | Description（建議填） |
|---|---|---|
| `read` | 可以登入 dashboard、用終端工具換取自己的 API key | `LLM Gateway: sign in to the dashboard` |
| `admin` | 可以開啟 admin 頁面 | `LLM Gateway: administer accounts, limits and models` |

**名稱要一字不差**：gateway 讀的就是這個 client 底下名為 `read` 和 `admin` 的角色。

### 角色名稱要不要取得正式一點？

不用，而且不建議改：

- 它們是 **client role**，在 Keycloak 裡的全名本來就是 `llm_gateway` / `read`，不會跟別的系統的角色混淆。Realm role 才需要靠命名來區分。
- gateway 刻意**不讀 realm role**：realm 層級叫 `admin` 的角色是別的系統的權限，不能因此打開 gateway 的 admin 頁面。

該做得正式的是**怎麼指派**，不是名稱：

| 建議 | 做法 |
|---|---|
| 用群組指派，不要逐人指派 | 建 `llm-gateway-users`、`llm-gateway-admins` 兩個群組，把 role 掛在群組上 |
| admin 群組要小，而且有名有姓 | 兩到三個人。admin 可以看到任何人的 API key、改任何人的額度 |
| 每個 role 和群組都填 Description | 之後做存取審查時，看得出這個權限是什麼 |
| 異動走申請流程 | 誰加入 admin 群組，在 Keycloak 的 Admin events 會留紀錄（Realm settings → Events 要開啟） |

如果公司的 Keycloak 有強制的角色命名規則（例如一定要有前綴），告訴我，gateway 可以加一個設定來對應，不需要你遷就。

### 把角色掛到群組上

*Groups* → *Create group* → `llm-gateway-admins` → 進入群組 → *Role mapping* → *Assign role* → 篩選改成 *Filter by clients* → 搜尋 `llm_gateway` → 勾 `admin` → *Assign*。再到 *Members* 加入管理員。

`read` 有兩種給法，擇一：

| 給誰 | 做法 |
|---|---|
| **只給申請的人** | 建群組 `llm-gateway-users`，掛上 `read`，把人加進去 |
| **全公司** | *Realm settings* → *User registration* → *Default roles* → *Assign role* → *Filter by clients* → `llm_gateway` 的 `read`。realm 裡每個人就都有 |

給全公司的話，任何人第一次登入就會自動在 gateway 建立帳號（用預設的每日額度）。這跟現在 AuthCenter 的行為是否一致，請先確認。

admin 的人**也要有 `read`**嗎？不用：gateway 裡 `admin` 包含 `read`。

## 4. 把部門放進 token（選用，建議做）

gateway 用部門代碼（`org_code`）決定 IP allowlist 的部門規則，以及 service account 的擁有者是否還在該部門。登入時從 token 讀取。

*Clients* → `llm_gateway` → *Client scopes* 分頁 → 點 `llm_gateway-dedicated` → *Add mapper* → *By configuration* → *User Attribute*

| 欄位 | 值 |
|---|---|
| Name | `org code` |
| User Attribute | Keycloak 使用者身上存放部門代碼的欄位名稱 |
| Token Claim Name | `org_code` |
| Claim JSON Type | `String` |
| Add to access token | **On** |
| Add to ID token / userinfo | Off |

不確定欄位叫什麼？先跳過，第 7 步的 preflight 做完後執行 `uv run python scripts/offboarding_check.py --everyone`，它會列出使用者身上有哪些欄位。

欄位的值必須跟現在 gateway 上看到的部門代碼是同一種格式（Users 表格上的那種），不然每個人登入後部門都會被改掉。

## 5. Realm 的兩個開關

*Realm settings* → *Login* 分頁：

| 設定 | 值 | 原因 |
|---|---|---|
| User registration | **Off** | 能自己註冊就能自己選 username |
| Edit username | **Off** | 能改 username 的人，可以把自己改成別人的帳號名稱，登入後就拿到那個人的 API key |

*Realm settings* → *Tokens* 分頁，記下 **Access Token Lifespan**（預設 5 分鐘）。切換時 oauth2-proxy 的更新間隔要比它短。

如果 realm 的使用者是從 AD / LDAP 同步來的，這兩項通常已經是關的。

## 6. 建立檢查用的 client

*Clients* → *Create client*

| 頁面 | 欄位 | 值 |
|---|---|---|
| General settings | Client ID | `llm-gateway-offboarding` |
| Capability config | Client authentication | **On** |
| | Service accounts roles | **勾選** |
| | Standard flow、Direct access grants | **都取消勾選** |

存檔後：

1. *Credentials* 分頁 → 複製 Client secret。
2. *Service accounts roles* 分頁 → *Assign role* → 選 client roles（對話框裡的 *Filter by clients*；較新的介面 *Assign role* 本身是選單，選 *Client roles*）→ 搜尋**角色名稱** `view-users`（搜尋框找不到 client 的名稱；結果那一列會標示它屬於 `realm-management`，在 `master` realm 操作的話是 `master-realm`），勾選：

| Role | 用途 | 什麼時候可以拿掉 |
|---|---|---|
| `view-users` | 讀使用者清單（誰被停用、部門） | 一直要留 |
| `view-clients` | preflight 檢查登入 client 的設定 | 切換完成後 |
| `view-realm` | preflight 檢查 token 效期與 realm 開關 | 切換完成後 |

這三個都是唯讀，改不了任何東西。後兩個只有 preflight 用得到；沒給的話 preflight 會把那幾項標成 `skip`，不會當成通過。

## 7. 填入設定，跑 preflight

在 gateway 主機的 `.env` 加上（**`SSO_PROVIDER` 先不要動**，登入還是 AuthCenter）：

```bash
# 登入（切換當天才生效）
KEYCLOAK_ISSUER_URL=https://<keycloak>/realms/<realm>
KEYCLOAK_CLIENT_ID=llm_gateway

# 離職與調職檢查
OFFBOARDING_KEYCLOAK_URL=https://<keycloak>/realms/<realm>
OFFBOARDING_KEYCLOAK_CLIENT_ID=llm-gateway-offboarding
OFFBOARDING_KEYCLOAK_CLIENT_SECRET=<第 6 步的 secret>
```

然後執行（只讀，不會改任何東西，也不用重啟）：

```bash
uv run python scripts/keycloak_preflight.py
```

它會逐項檢查並告訴你怎麼修：

| 區塊 | 檢查什麼 |
|---|---|
| Settings | 設定有沒有填、格式對不對 |
| Keycloak's certificate | 這台主機信不信任 Keycloak 的憑證；不信任的話是哪個 CA 簽的 |
| The realm | realm 自稱的 issuer 跟設定是否一字不差；簽章金鑰讀不讀得到 |
| oauth2-proxy | 切換當天 `deploy/.env` 要改成什麼 |
| Accounts | 哪些帳號有大寫、哪些在 Keycloak 找不到、哪些已被停用 |
| The sign-in client | client 的設定、redirect URI、兩個 role、誰有 admin、部門 mapper |
| A real token | 見下方 |

每一行的狀態：`ok` 通過；`FAIL` 一定要修；`WARN` 請看一下；`todo` 是切換當天要做的事；`skip` 是沒辦法檢查（會說原因），**不代表通過**。

### 簽章金鑰（JWKS）要設定嗎？

不用。每個 realm 都會自動把簽章用的公鑰公開在 `<realm URL>/protocol/openid-connect/certs`，gateway 自己去讀，金鑰輪替時也會自己更新。沒有金鑰檔要複製（AuthCenter 的 `keys/public.pem` 切換後就不再使用）。

唯一要設定的情況：gateway 連 Keycloak 用的位址，跟 token 裡 `iss` 的位址不一樣（例如對外是 `login.company.com`，gateway 只能走內部位址）。這時 `KEYCLOAK_ISSUER_URL` 填 token 裡的那個，另外設 `KEYCLOAK_JWKS_URL` 指向 gateway 連得到的位址。preflight 的 *The realm* 區塊會告訴你需不需要。

### 憑證是內部 CA 簽的

preflight 會顯示類似這樣：

```
[ FAIL ] TLS to keycloak.company.com:443: not trusted (unable to get local issuer certificate): ...
         → This host does not trust that CA. Save the certificate of CN=Company Root CA as a PEM file ...
```

處理方式：

1. 向管理 CA 的人要那張 CA 憑證（PEM 格式）。如果拿到的是 `.cer` / `.der`：`openssl x509 -inform der -in ca.cer -out ca.pem`。
2. 放到 gateway 主機，例如 `/etc/ssl/company-ca.pem`，在 `.env` 設定 `KEYCLOAK_CA_FILE=/etc/ssl/company-ca.pem`。離職檢查會沿用同一個檔案。
3. 再跑一次 preflight。它會檢查這個檔案：是不是 PEM、裡面是不是 CA、有沒有過期、有沒有不小心放了私鑰，以及用它能不能驗證 Keycloak。

拿不到 CA 憑證時，可以先把伺服器送出的 CA 存下來：

```bash
uv run python scripts/keycloak_preflight.py --save-ca keycloak-ca.pem
```

這份憑證**沒有經過驗證**。指令會印出每張的 SHA-256，一定要跟管理 CA 的人核對過才能使用。

兩種常見的狀況：

- **「the CA is trusted, but a certificate in the chain is refused」**：CA 是對的，但憑證本身不符合 RFC 5280（常見的是缺少 key identifier）。Python 3.13 之後預設會拒絕，gateway 也一樣。要請 CA 重新簽發憑證，換 CA 檔沒有用。
- **「None of them is a CA」**：你給的是伺服器自己的憑證，不是簽它的 CA。現在能用，但伺服器憑證一換新就會失效。

### 用一個真人的 token 做最後確認

設定對不對，最準的是拿一個真人的 access token 給 gateway 自己的檢查程式看。

不用登入就能做的版本：*Clients* → `llm_gateway` → *Client scopes* 分頁 → *Evaluate* → 選一個使用者 → *Generated access token*，把顯示的 JSON 存成檔案：

```bash
uv run python scripts/keycloak_preflight.py --token payload.json
```

它會告訴你這個人登入後會是哪個帳號、是不是 admin、部門是什麼、gateway 上有沒有這個帳號。請至少試一個一般使用者和一個 admin。

如果手上有真正簽過章的 access token，同樣用 `--token` 給它，還會多驗證簽章與效期（token 只有幾分鐘，拿到就要馬上跑）。token 請放在檔案或用 `--token -` 從 stdin 給，不要打在指令列上。

## 8. 開啟離職與調職檢查

preflight 的 *Accounts* 區塊沒問題後，在 `.env` 加上並重啟 gateway：

```bash
OFFBOARDING_MODE=monitor
# OFFBOARDING_KEYCLOAK_ORG_ATTR=<部門欄位>    # 要順便更新換部門的人才設
```

之後 admin 頁面的 *Needs attention* 會列出「登入已停用、帳號還有效」的人，由你手動停用。名單準確一段時間後再改成 `enforce`。細節見 [deploy/README.zh-TW.md](../deploy/README.zh-TW.md#離職者的帳號)。

這一步跟登入無關，可以比切換早很多天做。

## 9. 切換登入

事前：

- [ ] preflight 沒有 `FAIL`，`skip` 的項目都看過原因
- [ ] 用 `--token` 試過一個一般使用者和一個 admin
- [ ] *Accounts* 區塊列出的大寫帳號、Keycloak 找不到的帳號都處理完
- [ ] 至少一個你自己能登入的帳號有 `admin`
- [ ] 通知使用者：切換後要重新登入一次；API key 不受影響
- [ ] 有人在用終端工具登入（`POST /auth/api-key`）的話，工具要同時改成向 Keycloak 取得 token

**(1) oauth2-proxy：`deploy/.env`**

```bash
OIDC_ISSUER_URL=https://<keycloak>/realms/<realm>
OAUTH2_CLIENT_ID=llm_gateway
OAUTH2_CLIENT_SECRET=<第 2 步的 secret>
# 要比 Access Token Lifespan 短，不然第一個 token 過期後網頁全部變成 401
OAUTH2_COOKIE_REFRESH=4m
```

Keycloak 是內部 CA 的話，oauth2-proxy 也要拿到那張 CA 憑證。它的容器只認公開的 CA，**看不到這台主機的信任清單**，所以就算主機已經信任公司的 CA，容器還是不認得。沒給的話 oauth2-proxy 會起不來（`docker logs llm-gateway-oauth2-proxy` 顯示 `x509: certificate signed by unknown authority`），所有網頁都回 500。在 `deploy/.env` 填主機上 CA 憑證（PEM）的絕對路徑：

```bash
OAUTH2_PROXY_PROVIDER_CA_FILES=/etc/ssl/company-ca.pem
```

`deploy/docker-compose.yml` 會把這個檔案掛進容器的同一個路徑，並把變數傳進去。檔案要讓其他使用者讀得到（`chmod 644`），因為容器不是用 root 跑的。主機本身已經信任這個 CA 的話，可以直接用主機的 CA 總表（Debian / Ubuntu 是 `/etc/ssl/certs/ca-certificates.crt`，RHEL 是 `/etc/pki/tls/certs/ca-bundle.crt`）。不用時把整行拿掉，不要留空值。

preflight 會事先告訴你需不需要（*oauth2-proxy* → *Keycloak's CA*），也會實際用你給的檔案試一次。

**(2) gateway：`.env`**

```bash
SSO_PROVIDER=keycloak
```

**(3) 兩邊一起套用**（中間這幾秒網頁會回 401）

```bash
docker compose -f deploy/docker-compose.yml up -d    # 用容器跑 gateway 的主機：bash deploy/migration/compose.sh up -d
systemctl --user restart llm-gateway
```

**(4) 驗證**

- [ ] 用自己的帳號登入 dashboard：是原本的帳號，API key 沒變
- [ ] 有 `admin` 的帳號打得開 `/admin`；沒有的回 403
- [ ] 等超過 Access Token Lifespan（預設 5 分鐘）再重新整理一次，還是登入狀態
- [ ] 一個 API 請求照常成功（API key 不受影響）
- [ ] 再跑一次 `scripts/keycloak_preflight.py`：`SSO_PROVIDER` 顯示 `keycloak`，oauth2-proxy 區塊全部 `ok`

常見錯誤與處理在 [deploy/README.zh-TW.md](../deploy/README.zh-TW.md#將-sso-換成-keycloak) 的最後。

切換完成後，可以把第 6 步的 `view-clients`、`view-realm` 從檢查用 client 拿掉。

## 10. 退回 AuthCenter

1. `.env` 拿掉 `SSO_PROVIDER=keycloak`（`KEYCLOAK_*` 可以留著）。
2. `deploy/.env` 的 `OIDC_ISSUER_URL`、`OAUTH2_CLIENT_ID`、`OAUTH2_CLIENT_SECRET` 改回 AuthCenter 的值，拿掉 `OAUTH2_COOKIE_REFRESH`。
3. 同樣兩個指令套用。

帳號、key、用量都不會因為切換或退回而改變。離職檢查不受影響，可以繼續開著。
