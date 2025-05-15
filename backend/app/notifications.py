from sqlalchemy import select
from sqlmodel import Session
from firebase_admin import messaging, exceptions
import traceback #
from .db import engine, DeviceToken

def notify_all(spot_id: int):
    """Send an FCM notification to all registered device tokens by sending individually."""
    print(f"[notify_all] Attempting to send notification for spot_id: {spot_id}")
    
    device_tokens_str_list = []
    try:
        with Session(engine) as sess:
            db_rows = sess.exec(select(DeviceToken)).all()

            if not db_rows:
                print("[notify_all] No rows found in DB. Cannot send notifications.")
                return {"success_count": 0, "failure_count": 0, "responses": []} 

            for row in db_rows:
                if not row: 
                    print("[notify_all] Encountered an empty row, skipping.")
                    continue
                
                dt_instance = row[0] 
                if isinstance(dt_instance, DeviceToken) and \
                   hasattr(dt_instance, 'token') and \
                   isinstance(dt_instance.token, str) and \
                   dt_instance.token.strip():
                    device_tokens_str_list.append(dt_instance.token)
                else:
                    print(f"[notify_all] Warning: Expected DeviceToken instance in row, but got {type(dt_instance)}, or token attribute is invalid.")
            
            print(f"[notify_all] Extracted {len(device_tokens_str_list)} valid string tokens: {device_tokens_str_list}")

        if not device_tokens_str_list:
            print("[notify_all] No valid string tokens extracted after filtering. Cannot send notifications.")
            return {"success_count": 0, "failure_count": 0, "responses": []}

        success_count = 0
        failure_count = 0
        individual_responses = []

        # --- Create a new Message for each token --- #
        for token_str in device_tokens_str_list:
            message = messaging.Message(
                notification=messaging.Notification(
                    title="Parking Spot Available!",
                    body=f"Spot {spot_id} is now available.",
                ),
                token=token_str, 
            )
            
            try:
                response_str = messaging.send(message)
                success_count += 1
                individual_responses.append({"success": True, "message_id": response_str, "token": token_str}) 
            except exceptions.FirebaseError as fe_individual:
                print(f"[notify_all] Failed to send message to token {token_str}: {fe_individual}")
                failure_count += 1
                individual_responses.append({"success": False, "exception": str(fe_individual), "token": token_str})
            except Exception as e_individual:
                print(f"[notify_all] Generic exception sending to token {token_str}: {e_individual}")
                failure_count += 1
                individual_responses.append({"success": False, "exception": str(e_individual), "token": token_str})

        print(f"[notify_all] FCM send summary: SuccessCount={success_count}, FailureCount={failure_count}")
        return {"success_count": success_count, "failure_count": failure_count, "responses": individual_responses}

    except Exception as e: 
        print(f"[notify_all] Generic exception during token processing or setup: {e}")
        print(traceback.format_exc())
        return {"success_count": 0, "failure_count": len(device_tokens_str_list) if device_tokens_str_list else 0, "responses": []}