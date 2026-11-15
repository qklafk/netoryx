# Moving over from another bot

Russian: [ru/migrating.md](ru/migrating.md).

## From Bedolaga (Remnawave)

The import moves users and their subscriptions from a backup of the Bedolaga bot. Customers keep working: when the
Remnawave panel they use is connected to Netoryx, every subscription takes over the customer's existing panel account,
so the link already in their app stays the same.

What is moved:

- users with a Telegram id: name, language, balance (as a ledger entry), who invited whom;
- for each user the subscription with the latest end date: end date, status (active, expired, suspended), trial flag,
  device limit and traffic limit.

What is not moved: payments history, promo codes, tariffs, campaigns, e-mail-only accounts (no Telegram id) and deleted
users. Plans and prices are set up in Netoryx anew.

### Steps

1. Make a backup in the Bedolaga admin and download the `.tar.gz`. A bare `database.sql`,
   `database.json` or `database.sqlite` from it works too.
2. Install Netoryx (`--panel none` if the Remnawave panel stays where it is) and connect that panel in the web admin
   (**Servers**). Note its id.
3. Copy the backup to the server and count first (nothing is changed):

   ```bash
   sudo netoryx import-bedolaga /root/backup.tar.gz --panel-id 1
   ```

   The report shows how many users and subscriptions will be moved, how many keep their link (`links_kept`) and how many
   will get a new one (`new_links`: no panel account was found for them).
4. Import:

   ```bash
   sudo netoryx import-bedolaga /root/backup.tar.gz --panel-id 1 --apply
   ```

5. Switch the bot: put the same bot token into Netoryx (`BOT_TOKEN` in `/opt/netoryx/.env`, then `sudo netoryx restart app`)
   and stop the old bot, so customers find their subscription in the same chat.

A second run imports only users that are not here yet: users that already exist are never changed. Without `--panel-id`
the subscriptions get new panel accounts on the next sync and customers must add the new link from the bot.
