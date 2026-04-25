# Famousbytee API Documentation

This API is designed to be used by Android, iOS, and other client applications.

## Base URL
`http://famousbytee.arisdev.web.id/api`

## Authentication
The API uses JSON Web Tokens (JWT). Most endpoints require an `Authorization` header.

**Header Format:**
`Authorization: Bearer <your_access_token>`

---

## Endpoints

### 1. Login
Authenticate and receive an access token.

- **URL:** `/login`
- **Method:** `POST`
- **Payload:**
  ```json
  {
    "username": "your_username",
    "password": "your_password"
  }
  ```
- **Response (200 OK):**
  ```json
  {
    "access_token": "eyJ0eXAi...",
    "user": {
      "id": 1,
      "username": "admin",
      "full_name": "Administrator",
      "role": "Admin"
    }
  }
  ```

### 2. Get Profile
Get details about the logged-in user and their student status.

- **URL:** `/profile`
- **Method:** `GET`
- **Auth Required:** Yes
- **Response (200 OK):** Includes personal financial info if the user is linked to a student record.

### 3. Announcements
Fetch all announcements.

- **URL:** `/announcements`
- **Method:** `GET`
- **Auth Required:** Yes

### 4. Schedules
Fetch all class schedules.

- **URL:** `/schedules`
- **Method:** `GET`
- **Auth Required:** Yes

### 5. Funds Summary
Get the current balance and financial summary.

- **URL:** `/funds/summary`
- **Method:** `GET`
- **Auth Required:** Yes

### 6. Gallery
Fetch published gallery photos.

- **URL:** `/gallery`
- **Method:** `GET`
- **Auth Required:** Yes

---

## Implementation Tips for Mobile
1. **Security**: Always use HTTPS in production.
2. **Persistence**: Store the `access_token` securely (e.g., EncryptedSharedPreferences on Android, Keychain on iOS).
3. **CORS**: CORS is enabled on the server side to allow requests from any origin.
