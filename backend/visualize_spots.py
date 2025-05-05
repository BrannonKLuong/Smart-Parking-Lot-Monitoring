import cv2, json, random

# load spots
with open("backend/spots.json") as f:
    spots = json.load(f)["spots"]

# grab one frame
cap = cv2.VideoCapture(0)
ret, img = cap.read()
cap.release()

# draw all, each in its own color
for spot in spots:
    x, y, w, h = spot["bbox"]

    # deterministic “random” color per spot id
    rng = random.Random(spot["id"])
    color = (
        rng.randint(0, 255),
        rng.randint(0, 255),
        rng.randint(0, 255),
    )

    # draw rectangle and label with that color
    cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)
    cv2.putText(
        img,
        str(spot["id"]),
        (x, y - 10),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        color,
        2,
    )

# show it
cv2.imshow("ROIs", img)
cv2.waitKey(0)
cv2.destroyAllWindows()